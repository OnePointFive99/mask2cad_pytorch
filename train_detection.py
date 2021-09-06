r'''PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        train.py ... --world-size $NGPU

The default hyperparameters are tuned for training on 8 gpus and 2 images per gpu.
    --lr 0.02 --batch-size 2 --world-size 8
If you use different number of gpus, the learning rate should be changed to 0.02/8*$NGPU.

On top of that, for training Faster/Mask R-CNN, the default hyperparameters are
    --epochs 26 --lr-steps 16 22 --aspect-ratio-group-factor 3

Also, if you train Keypoint R-CNN, the default hyperparameters are
    --epochs 46 --lr-steps 36 43 --aspect-ratio-group-factor 3
Because the number of images is smaller in the person keypoint subset of COCO,
the number of epochs should be adapted so that we have the same number of iterations.
'''
import argparse
import datetime
import os
import math
import sys
import time

import torch
import torch.utils.data
import torchvision
import torchvision.models.detection
import torchvision.models.detection.mask_rcnn

import datasets
import samplers
import transforms
import utils

from coco_utils import get_coco, get_coco_kp, get_coco_api_from_dataset
from coco_eval import CocoEvaluator

def train_one_epoch(model, optimizer, data_loader, device, epoch, print_freq):
    model.train()
    metric_logger = utils.MetricLogger(delimiter='  ')
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)

    lr_scheduler = None
    if epoch == 0:
        warmup_factor = 1. / 1000
        warmup_iters = min(1000, len(data_loader) - 1)

        lr_scheduler = utils.warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for images, targets in metric_logger.log_every(data_loader, print_freq, header):
        images = list(image.to(device) for image in images)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        loss_dict = model(images, targets)

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())

        if not torch.isfinite(losses_reduced):
            loss_value = losses_reduced.item()
            print('Loss is {}, stopping training'.format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()

        if lr_scheduler is not None:
            lr_scheduler.step()

        metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]['lr'])

    return metric_logger


def _get_iou_types(model):
    model_without_ddp = model
    if isinstance(model, torch.nn.parallel.DistributedDataParallel):
        model_without_ddp = model.module
    iou_types = ['bbox']
    if isinstance(model_without_ddp, torchvision.models.detection.MaskRCNN):
        iou_types.append('segm')
    if isinstance(model_without_ddp, torchvision.models.detection.KeypointRCNN):
        iou_types.append('keypoints')
    return iou_types


@torch.no_grad()
def evaluate(model, data_loader, device):
    n_threads = torch.get_num_threads()
    # FIXME remove this and make paste_masks_in_image run on the GPU
    torch.set_num_threads(1)
    cpu_device = torch.device('cpu')
    model.eval()
    metric_logger = utils.MetricLogger(delimiter='  ')
    header = 'Test:'

    coco = get_coco_api_from_dataset(data_loader.dataset)
    iou_types = _get_iou_types(model)
    coco_evaluator = CocoEvaluator(coco, iou_types)

    for images, targets in metric_logger.log_every(data_loader, 100, header):
        images = list(img.to(device) for img in images)

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        model_time = time.time()
        outputs = model(images)

        outputs = [{k: v.to(cpu_device) for k, v in t.items()} for t in outputs]
        model_time = time.time() - model_time

        res = {target['image_id'].item(): output for target, output in zip(targets, outputs)}
        evaluator_time = time.time()
        coco_evaluator.update(res)
        evaluator_time = time.time() - evaluator_time
        metric_logger.update(model_time=model_time, evaluator_time=evaluator_time)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('Averaged stats:', metric_logger)
    coco_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    coco_evaluator.accumulate()
    coco_evaluator.summarize()
    torch.set_num_threads(n_threads)
    return coco_evaluator

def get_transform(train, data_augmentation):
    return transforms.DetectionPresetTrain(data_augmentation) if train else transforms.DetectionPresetEval()


def main(args):
    print(args)
    
    os.makedirs(args.output_dir, exist_ok = True)
    utils.init_distributed_mode(args)

    train_dataset = datasets.Pix3D(args.dataset_root, split_path = args.train_metadata_path)
    val_dataset = datasets.Pix3D(args.dataset_root, split_path = args.val_metadata_path)
    
    num_classes = len(train_dataset.categories)
    breakpoint()

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
    else:
        train_sampler = torch.utils.data.RandomSampler(train_dataset)
        val_sampler = torch.utils.data.SequentialSampler(val_dataset)

    if args.aspect_ratio_group_factor >= 0:
        group_ids = samplers.create_aspect_ratio_groups(train_dataset, k = args.aspect_ratio_group_factor)
        train_batch_sampler = samplers.GroupedBatchSampler(train_sampler, group_ids, args.batch_size)
    else:
        train_batch_sampler = torch.utils.data.BatchSampler(train_sampler, args.batch_size, drop_last=True)

    train_data_loader  =  torch.utils.data.DataLoader(train_dataset, batch_sampler = train_batch_sampler, num_workers = args.workers, collate_fn = datasets.collate_fn)
    val_data_loader  =  torch.utils.data.DataLoader(val_dataset, batch_size = 1, sampler = val_sampler, num_workers = args.workers, collate_fn = datasets.collate_fn)

    kwargs = {
        'trainable_backbone_layers': args.trainable_backbone_layers
    }
    if 'rcnn' in args.model:
        if args.rpn_score_thresh is not None:
            kwargs['rpn_score_thresh'] = args.rpn_score_thresh
    model = getattr(torchvision.models.detection, args.model)(num_classes=num_classes, pretrained=args.pretrained, **kwargs)
    model.to(args.device)
    if args.distributed and args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    if args.lr_scheduler == 'MultiStepLR':
        lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.lr_steps, gamma=args.lr_gamma)
    elif args.lr_scheduler == 'CosineAnnealingLR':
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        args.start_epoch = checkpoint['epoch'] + 1

    if args.test_only:
        evaluate(model, val_data_loader, device=device)
        return

    print('Start training')
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(model, optimizer, train_data_loader, device, epoch, args.print_freq)
        lr_scheduler.step()
        if args.output_dir:
            checkpoint = dict(
                model = model_without_ddp.state_dict(),
                optimizer = optimizer.state_dict(),
                lr_scheduler = lr_scheduler.state_dict(),
                args = args,
                epoch = epoch
            )
            utils.save_on_master(
                checkpoint,
                os.path.join(args.output_dir, 'model_{}.pt'.format(epoch)))
            utils.save_on_master(
                checkpoint,
                os.path.join(args.output_dir, 'checkpoint.pt'))

        # evaluate after every epoch
        evaluate(model, val_data_loader, device=args.device)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--dataset-root', default = 'data/common/pix3d')
    parser.add_argument('--train-metadata-path', default = 'data/common/pix3d_splits/pix3d_s1_train.json')
    parser.add_argument('--val-metadata-path', default = 'data/common/pix3d_splits/pix3d_s1_test.json')
    parser.add_argument('--dataset', default='Pix3D')
    
    parser.add_argument('--model', default='maskrcnn_resnet50_fpn')
    parser.add_argument('--device', default='cpu', help='device')
    parser.add_argument('--batch-size', '-b', default=2, type=int, help='images per gpu, the total batch size is $NGPU x batch_size')
    parser.add_argument('--epochs', default=26, type=int)
    parser.add_argument( '--num-workers', '-j', default=4, type=int)
    parser.add_argument('--lr', default=0.02, type=float, help='initial learning rate, 0.02 is the default value for training on 8 gpus and 2 images_per_gpu')
    parser.add_argument('--momentum', default=0.9, type=float)
    parser.add_argument('--weight-decay', default=1e-4, type=float)
    parser.add_argument('--lr-scheduler', default = 'MultiStepLR', choices = ['MultiStepLR', 'CosineAnnealingLR'])
    parser.add_argument('--lr-step-size', default=8, type=int, help='decrease lr every step-size epochs (multisteplr scheduler only)')
    parser.add_argument('--lr-steps', default=[16, 22], nargs='+', type=int, help='decrease lr every step-size epochs (multisteplr scheduler only)')
    parser.add_argument('--lr-gamma', default=0.1, type=float, help='decrease lr by a factor of lr-gamma (multisteplr scheduler only)')

    parser.add_argument('--print-freq', default=20, type=int)
    parser.add_argument('--output-dir', default='data')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start-epoch', default=0, type=int)
    parser.add_argument('--aspect-ratio-group-factor', default=3, type=int)
    parser.add_argument('--rpn-score-thresh', default=None, type=float, help='rpn score threshold for faster-rcnn')
    parser.add_argument('--trainable-backbone-layers', default=None, type=int, help='number of trainable layers of backbone')
    parser.add_argument('--data-augmentation', default='hflip', help='data augmentation policy (default: hflip)')
    parser.add_argument('--sync-bn', action='store_true')
    parser.add_argument('--test-only', action='store_true')
    parser.add_argument('--pretrained', action='store_true')

    parser.add_argument('--world-size', default=1, type=int, help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')

    main(parser.parse_args())
