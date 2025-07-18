#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import argparse
from logging import Logger
import os
import yaml
import os.path as osp
from pathlib import Path
import torch
import torch.distributed as dist
import sys
import datetime

ROOT = os.getcwd()
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from yolov6.core.engine import Trainer
from yolov6.utils.config import Config
from yolov6.utils.events import LOGGER, save_yaml
from yolov6.utils.envs import get_envs, select_device, set_random_seed
from yolov6.utils.general import increment_name, find_latest_checkpoint, check_img_size

import functools

# Store the original torch.load function
_original_torch_load = torch.load

def custom_torch_load(*args, **kwargs):
    # If 'weights_only' is not explicitly set, set it to False
    if 'weights_only' not in kwargs:
        kwargs['weights_only'] = False
    print(f"DEBUG: torch.load called with weights_only={kwargs.get('weights_only', 'default_was_True_now_False')}") # Optional: for debugging
    return _original_torch_load(*args, **kwargs)

# Monkey-patch torch.load
torch.load = custom_torch_load
# --- END OF CUSTOM PATCH ---


def get_args_parser(add_help=True):
    parser = argparse.ArgumentParser(description='YOLOv6 PyTorch Training', add_help=add_help)
    parser.add_argument('--data-path', default='./data/coco.yaml', type=str, help='path of dataset')
    parser.add_argument('--conf-file', default='./configs/yolov6n.py', type=str, help='experiments description file')
    parser.add_argument('--img-size', default=640, type=int, help='train, val image size (pixels)')
    parser.add_argument('--rect', action='store_true', help='whether to use rectangular training, default is False')
    parser.add_argument('--batch-size', default=32, type=int, help='total batch size for all GPUs')
    parser.add_argument('--epochs', default=400, type=int, help='number of total epochs to run')
    parser.add_argument('--workers', default=8, type=int, help='number of data loading workers (default: 8)')
    parser.add_argument('--device', default='0', type=str, help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--eval-interval', default=20, type=int, help='evaluate at every interval epochs')
    parser.add_argument('--eval-final-only', action='store_true', help='only evaluate at the final epoch')
    parser.add_argument('--heavy-eval-range', default=50, type=int,
                        help='evaluating every epoch for last such epochs (can be jointly used with --eval-interval)')
    parser.add_argument('--check-images', action='store_true', help='check images when initializing datasets')
    parser.add_argument('--check-labels', action='store_true', help='check label files when initializing datasets')
    parser.add_argument('--output-dir', default='./runs/train', type=str, help='path to save outputs')
    parser.add_argument('--name', default='exp', type=str, help='experiment name, saved to output_dir/name')
    parser.add_argument('--dist_url', default='env://', type=str, help='url used to set up distributed training')
    parser.add_argument('--gpu_count', type=int, default=0)
    parser.add_argument('--local_rank', type=int, default=-1, help='DDP parameter')
    parser.add_argument('--resume', nargs='?', const=True, default=False, help='resume the most recent training')
    parser.add_argument('--write_trainbatch_tb', action='store_true', help='write train_batch image to tensorboard once an epoch, may slightly slower train speed if open')
    parser.add_argument('--stop_aug_last_n_epoch', default=15, type=int, help='stop strong aug at last n epoch, neg value not stop, default 15')
    parser.add_argument('--save_ckpt_on_last_n_epoch', default=-1, type=int, help='save last n epoch even not best or last, neg value not save')
    parser.add_argument('--distill', action='store_true', help='distill or not')
    parser.add_argument('--distill_feat', action='store_true', help='distill featmap or not')
    parser.add_argument('--quant', action='store_true', help='quant or not')
    parser.add_argument('--calib', action='store_true', help='run ptq')
    parser.add_argument('--teacher_model_path', type=str, default=None, help='teacher model path')
    parser.add_argument('--temperature', type=int, default=20, help='distill temperature')
    parser.add_argument('--fuse_ab', action='store_true', help='fuse ab branch in training process or not')
    parser.add_argument('--bs_per_gpu', default=32, type=int, help='batch size per GPU for auto-rescale learning rate, set to 16 for P6 models')
    parser.add_argument('--specific-shape', action='store_true', help='rectangular training')
    parser.add_argument('--height', type=int, default=None, help='image height of model input')
    parser.add_argument('--width', type=int, default=None, help='image width of model input')
    parser.add_argument('--cache-ram', action='store_true', help='whether to cache images into RAM to speed up training')
    return parser


def check_and_init(args):
    '''check config files and device.'''
    # check files
    master_process = args.rank == 0 if args.world_size > 1 else args.rank == -1
    if args.resume:
        # args.resume can be a checkpoint file path or a boolean value.
        checkpoint_path = args.resume if isinstance(args.resume, str) else find_latest_checkpoint()
        assert os.path.isfile(checkpoint_path), f'the checkpoint path is not exist: {checkpoint_path}'
        LOGGER.info(f'Resume training from the checkpoint file :{checkpoint_path}')
        resume_opt_file_path = Path(checkpoint_path).parent.parent / 'args.yaml'
        if osp.exists(resume_opt_file_path):
            with open(resume_opt_file_path) as f:
                args = argparse.Namespace(**yaml.safe_load(f))  # load args value from args.yaml
        else:
            LOGGER.warning(f'We can not find the path of {Path(checkpoint_path).parent.parent / "args.yaml"},'\
                           f' we will save exp log to {Path(checkpoint_path).parent.parent}')
            LOGGER.warning(f'In this case, make sure to provide configuration, such as data, batch size.')
            args.save_dir = str(Path(checkpoint_path).parent.parent)
        args.resume = checkpoint_path  # set the args.resume to checkpoint path.
    else:
        args.save_dir = str(increment_name(osp.join(args.output_dir, args.name)))
        if master_process:
            os.makedirs(args.save_dir)

    # check specific shape
    if args.specific_shape:
        if args.rect:
            LOGGER.warning('You set specific shape, and rect to True is needless. YOLOv6 will use the specific shape to train.')
        args.height = check_img_size(args.height, 32, floor=256)  # verify imgsz is gs-multiple
        args.width = check_img_size(args.width, 32, floor=256)
    else:
        args.img_size = check_img_size(args.img_size, 32, floor=256)

    cfg = Config.fromfile(args.conf_file)
    if not hasattr(cfg, 'training_mode'):
        setattr(cfg, 'training_mode', 'repvgg')
    # check device
    device = select_device(args.device)
    # set random seed
    set_random_seed(1+args.rank, deterministic=(args.rank == -1))
    # save args
    if master_process:
        save_yaml(vars(args), osp.join(args.save_dir, 'args.yaml'))

    return cfg, device, args


def main(args):
    '''main function of training'''
    # Setup
    args.local_rank, args.rank, args.world_size = get_envs()
    cfg, device, args = check_and_init(args)
    # reload envs because args was chagned in check_and_init(args)
    args.local_rank, args.rank, args.world_size = get_envs()
    LOGGER.info(f'training args are: {args}\n')
    if args.local_rank != -1: # if DDP mode
        torch.cuda.set_device(args.local_rank)
        device = torch.device('cuda', args.local_rank)
        LOGGER.info('Initializing process group... ')
        dist.init_process_group(backend="nccl" if dist.is_nccl_available() else "gloo", \
                init_method=args.dist_url, rank=args.local_rank, world_size=args.world_size,timeout=datetime.timedelta(seconds=7200))

    # Start
    trainer = Trainer(args, cfg, device)
    # PTQ
    if args.quant and args.calib:
        trainer.calibrate(cfg)
        return
    trainer.train()

    # End
    if args.world_size > 1 and args.rank == 0:
        LOGGER.info('Destroying process group... ')
        dist.destroy_process_group()


if __name__ == '__main__':
    args = get_args_parser().parse_args()
    main(args)
