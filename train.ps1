cd src
python main.py single_hand --exp_id synthhands --dataset active_hand --arch movenet --batch_size 24  --lr 5e-4 --gpus 0 --num_epochs 50 --lr_step 30 --num_workers 4