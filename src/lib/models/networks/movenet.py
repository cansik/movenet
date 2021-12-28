# ------------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Bin Xiao (Bin.Xiao@microsoft.com)
# Modified by Dequan Wang and Xingyi Zhou
# Modified by Min Li
# ------------------------------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import math
import logging

import cv2

from matplotlib import pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from .backbone_utils import mobilenet_backbone
import torch.utils.model_zoo as model_zoo
from opts import opts
BN_MOMENTUM = 0.1
logger = logging.getLogger(__name__)


class MoveNet(nn.Module):
    '''
    MoveNet from Goolge. Please refer their blog: https://blog.tensorflow.org/2021/05/next-generation-pose-detection-with-movenet-and-tensorflowjs.html

    '''
    def __init__(self, backbone, heads, head_conv, ft_size=48):
        super(MoveNet, self).__init__()
        self.out_channels = 24
        self.backbone = backbone
        self.heads = heads
        self.ft_size = ft_size
        self.weight_to_center = self._generate_center_dist(self.ft_size).unsqueeze(2)
 
        self.dist_y, self.dist_x = self._generate_dist_map(self.ft_size)
        self.index_17 = torch.arange(0, 17).float()

        for head in self.heads:
            classes = self.heads[head]
            if head_conv > 0:
                fc = nn.Sequential(
                  nn.Conv2d(self.out_channels, self.out_channels, 3, padding=1, groups=self.out_channels, bias=True),
                  nn.Conv2d(self.out_channels, head_conv, 1, 1, 0, bias=True),
                  nn.ReLU(inplace=True),
                  nn.Conv2d(head_conv, classes, 
                    kernel_size=1, stride=1, 
                    padding=0, bias=True))
            else:
                fc = nn.Conv2d(64, classes, 
                  kernel_size=1, stride=1, 
                  padding=0, bias=True)
            self.__setattr__(head, fc)


    def forward(self, x):
        # conv forward
        # x  = x * 0.007843137718737125 - 1.0
        # specify the device
        device = x.device
        self.weight_to_center = self.weight_to_center.to(device)
        self.dist_y, self.dist_x = self.dist_y.to(device), self.dist_x.to(device)
        # x = (1,24,64,64)
        x = self.backbone(x)
        ret = {}
        # x = (1,24,64,64)
        # self.heads = {'hm': 1, 'hps': 34, 'hm_hp': 17, 'hp_offset': 34}
        for head in self.heads:
            # self.__getattr__('hps') return Sequential(Conv2d)(x) =>
            ret[head] = self.__getattr__(head)(x)

        return [ret]

    def decode(self, x,name):
        kpt_heatmap, center, kpt_regress, kpt_offset = x['hm_hp'].squeeze(0).permute((1, 2, 0)), x['hm'].squeeze(0).permute((1, 2, 0)), x['hps'].squeeze(0).permute((1, 2, 0)), x['hp_offset'].squeeze(0).permute((1, 2, 0))
        # kpt_heatmap(17,64,64)
        # center(64,64,1)
        # kpt_regress(64,64,34)
        # kpt_offset(64,64,34)

         # pose decode
        kpt_heatmap = torch.sigmoid(kpt_heatmap)

        #np.save('/Users/rachel/PycharmProjects/movenet/experiments/1222test/npy/'+name+'.npy', kpt_heatmap)
        center = torch.sigmoid(center)

        ct_ind = self._top_with_center(center)

        kpt_coor = self._center_to_kpt(kpt_regress, ct_ind)
        # kpt_top_inds is using regress calc to heatmap
        kpt_top_inds = self._kpt_from_heatmap(kpt_heatmap, kpt_coor)
        # make offset as 0
        # kpt_offset=torch.zeros(64,34,34)
        kpt_with_conf1 = self._kpt_from_offset(kpt_offset, kpt_top_inds[0], kpt_heatmap, self.ft_size)
        kpt_with_conf2 = self._kpt_from_offset(kpt_offset, kpt_top_inds[1], kpt_heatmap, self.ft_size)
        # kpt_list = [kpt_with_conf1,kpt_with_conf2]
        kpt_list = [kpt_with_conf1,kpt_with_conf2]
        #return kpt_with_ckonf1
        return kpt_list

        
    def _draw(self, ft):
        plt.imshow(ft.numpy().reshape(self.ft_size, self.ft_size))
        plt.show()

    def _generate_center_dist(self, ft_size=48, delta=1.8):
        weight_to_center = torch.zeros((int(ft_size), int(ft_size)))
        y, x = np.ogrid[0:ft_size, 0:ft_size]
        center_y, center_x = ft_size / 2.0, ft_size/ 2.0
        y = y - center_y
        x = x - center_x
        weight_to_center = 1 / (np.sqrt(y * y + x * x) + delta)
        weight_to_center = torch.from_numpy(weight_to_center)
        return weight_to_center

    def _generate_dist_map(self, ft_size=48):
        y, x = np.ogrid[0:ft_size, 0:ft_size]
        y = torch.from_numpy(np.repeat(y, ft_size, axis=1)).unsqueeze(2).float()
        x = torch.from_numpy(np.repeat(x, ft_size, axis=0)).unsqueeze(2).float()

        return y, x


    def _top_with_center(self, center):
        scores = center * self.weight_to_center

        top_ind = torch.argmax(scores.view(1, self.ft_size * self.ft_size, 1), dim=1)
        return top_ind

    def _center_to_kpt(self, kpt_regress, ct_ind, ft_size=48):
        ct_y = torch.div(ct_ind, ft_size, rounding_mode='floor')
        # ct_y = (ct_ind.float() / ft_size).int().float()
        ct_x = ct_ind - ct_y * ft_size

        kpt_regress = kpt_regress.view(-1, 17, 2)
        ct_ind = ct_ind.unsqueeze(2).expand(ct_ind.size(0), 17, 2)
        kpt_coor = kpt_regress.gather(0, ct_ind).squeeze(0)
        
        kpt_coor = kpt_coor + torch.cat((ct_y, ct_x), dim=1)
        
        return kpt_coor

    def _kpt_from_heatmap(self, kpt_heatmap, kpt_coor):
        # kpt_heatmap: (64,64,17)
        # be 34
        y = self.dist_y - kpt_coor[:, 0].reshape(1, 1, 17)
        x = self.dist_x - kpt_coor[:, 1].reshape(1, 1, 17)
        #dist_weight = torch.sqrt(y * y + x * x) + 1.8
        dist_weight = 1
        scores = kpt_heatmap / dist_weight
        scores = scores.reshape((1, self.ft_size * self.ft_size, 17))
        # tensor(1,17)
        top_inds = torch.argmax(scores, dim=1)
        # shape(1,2,17)
        second_inds = []
        top2_inds = torch.topk(scores,5,dim=1).indices
        x = [[int(top2_inds[0][j][i]) for i in range(17)] for j in range(5)]
        indexes = [[x[i][j] for i in range(5)] for j in range(17)]
        for i in range(len(indexes)):
            base = indexes[i][0]
            y = []
            for j in indexes[i][1:]:
                if(abs(j-base)>1):
                    y.append(j)
            second_inds.append(y)
        second_inds = torch.as_tensor([k[0] for k in second_inds]).resize(1,17)
        ret_top2_inds = []
        ret_top2_inds.append(top_inds)
        ret_top2_inds.append(second_inds)
        #top_and_second = top_inds+n_second_inds
        return ret_top2_inds
    # heatmap: 64*64
    # kpt_offset = (4096,17,2)
    # kpt_top_inds = (1,17,2)
    # hpt_heatmap = (4076,17)

    def _kpt_from_offset(self, kpt_offset, kpt_top_inds, kpt_heatmap, size=48):
        kpts_ys = torch.div(kpt_top_inds, size, rounding_mode='floor')
        # kpts_ys = (kpt_top_inds.float() / size).int().float()
        kpts_xs = kpt_top_inds - kpts_ys * size
        kpt_coordinate = torch.stack((kpts_ys.squeeze(0), kpts_xs.squeeze(0)), dim=1)

        kpt_heatmap = kpt_heatmap.view(-1, 17)

        kpt_conf = kpt_heatmap.gather(0, kpt_top_inds).squeeze(0)

        kpt_offset = kpt_offset.view(-1, 17, 2)
        kpt_top_inds = kpt_top_inds.unsqueeze(2).expand(kpt_top_inds.size(0), 17, 2)
        kpt_offset_yx = kpt_offset.gather(0, kpt_top_inds).squeeze(0)

        kpt_coordinate= (kpt_offset_yx + kpt_coordinate) * (1/size)
        kpt_with_conf = torch.cat([kpt_coordinate, kpt_conf.unsqueeze(1)], dim=1).reshape((1, 1, 17, 3))

        return kpt_with_conf




# def get_pose_net(heads, head_conv=96, froze_backbone=True):
#     backbone = mobilenet_backbone('mobilenet_v2', pretrained=False, fpn=True)
#     if froze_backbone:
#         for param in backbone.parameters():
#             param.requires_grad = False
#     model = MoveNet(backbone, heads, head_conv=head_conv)
#     return model

def get_pose_net(heads, head_conv=96, froze_backbone=True, model_type = 'lighting'):
    backbone = mobilenet_backbone('mobilenet_v2', pretrained=False, fpn=True, trainable_layers=0, model_type = model_type)
    if froze_backbone:
        for param in backbone.parameters():
            param.requires_grad = False
    if model_type == 'lighting':
        ft_size = 48
    else:
        ft_size = 64
    model = MoveNet(backbone, heads, head_conv=head_conv, ft_size = ft_size)
    # froze
    opt = opts().parse()
    fix_heads = opt.fix_heads
    for k, v in model.named_parameters():
        head_name = k.split('.')[0]
        if head_name in fix_heads:
            v.requires_grad = False
    '''for k,v in model.named_parameters():
        head_name = k.split('.')[0]
        if head_name == 'hm' or head_name == 'hps':
            v.requires_grad = False'''

    return model