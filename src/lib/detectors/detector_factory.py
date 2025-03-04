from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


from .multi_pose import MultiPoseDetector
from .single_pose import SinglePoseDetector
from .single_hand import SingleHandDetector

detector_factory = {
  'multi_pose': MultiPoseDetector,
  'single_pose': SinglePoseDetector,
  'single_hand': SingleHandDetector,
}
