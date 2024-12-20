"""
USAGE

# training with Faster RCNN ResNet50 FPN model without mosaic or any other augmentation:
python train.py --model fasterrcnn_resnet50_fpn --epochs 2 --data data_configs/voc.yaml --mosaic 0 --batch 4

# Training on ResNet50 FPN with custom project folder name with mosaic augmentation (ON by default):
python train.py --model fasterrcnn_resnet50_fpn --epochs 2 --data data_configs/voc.yaml --name resnet50fpn_voc --batch 4

# Training on ResNet50 FPN with custom project folder name with mosaic augmentation (ON by default) and added training augmentations:
python train.py --model fasterrcnn_resnet50_fpn --epochs 2 --use-train-aug --data data_configs/voc.yaml --name resnet50fpn_voc --batch 4

# Distributed training:
export CUDA_VISIBLE_DEVICES=0,1
python -m torch.distributed.launch --nproc_per_node=2 --use_env train.py --data data_configs/smoke.yaml --epochs 100 --model fasterrcnn_resnet50_fpn --name smoke_training --batch 16
"""

import torch
import argparse
import yaml
import numpy as np
import torchinfo
import os
import pandas as pd
import torch
import torch.nn.functional as F
from torch_utils.engine import utils, evaluate, train_one_epoch
from torch.utils.data import distributed, RandomSampler, SequentialSampler
from datasets import create_train_dataset, create_valid_dataset, create_train_loader, create_valid_loader
from models.create_fasterrcnn_model import create_model
from utils.general import (
    set_training_dir, Averager, 
    save_model, save_loss_plot,
    show_tranformed_image,
    save_mAP, save_model_state, SaveBestModel,
    yaml_save, init_seeds
)
from utils.logging import (
    set_log, coco_log, set_summary_writer, 
    tensorboard_loss_log, tensorboard_map_log, #csv_log,
    wandb_log, wandb_save_model, wandb_init
)


torch.multiprocessing.set_sharing_strategy('file_system')

RANK = int(os.getenv('RANK', -1))

# For same annotation colors each time.
np.random.seed(99)

def parse_opt():
    # Construct the argument parser.
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--model', default='fasterrcnn_resnet50_fpn_v2',
                        help='name of the model')
    parser.add_argument('--data', default=None, 
                        help='path to the data config file')
    parser.add_argument('-d', '--device', default='cuda', 
                        help='computation/training device, default is GPU if GPU present')
    parser.add_argument('-e', '--epochs', default=5, type=int, 
                        help='number of epochs to train for')
    parser.add_argument('-j', '--workers', default=4, type=int, 
                        help='number of workers for data processing/transforms/augmentations')
    parser.add_argument('-b', '--batch', default=4, type=int, 
                        help='batch size to load the data')
    parser.add_argument('--lr', default=0.001, type=float, 
                        help='learning rate for the optimizer', )
    parser.add_argument('-ims', '--imgsz', default=640, type=int, 
                        help='image size to feed to the network')
    parser.add_argument('-n', '--name',default=None,type=str,
                        help='training result dir name in outputs/training, (default res_#)')
    parser.add_argument('-vt', '--vis-transformed', dest='vis_transformed', action='store_true', 
                        help='visualize transformed images fed to the network')
    parser.add_argument('--mosaic', default=0.0, type=float, 
                        help='probability of applying mosaic, (default, always apply)')
    parser.add_argument('-uta', '--use-train-aug', dest='use_train_aug', action='store_true', 
                        help='whether to use train augmentation, blur, gray,brightness contrast, color jitter, random gammaall at once')
    parser.add_argument('-ca', '--cosine-annealing', dest='cosine_annealing', action='store_true', 
                        help='use cosine annealing warm restarts' )
    parser.add_argument('-w', '--weights', default=None, type=str, 
                        help='path to model weights if using pretrained weights' )    
    parser.add_argument('-r', '--resume-training', dest='resume_training', action='store_true', 
                        help='whether to resume training, if true, loads previous training plots and epochs and also loads the otpimizer state dictionary' )
    parser.add_argument('-st', '--square-training', dest='square_training', action='store_true', 
                        help='Resize images to square shape instead of aspect ratio resizing for single image training. For mosaic training, this resizes single images to square shape first then puts them on a square canvas.' )
    parser.add_argument('--world-size', default=1, type=int, 
                        help='number of distributed processes' )
    parser.add_argument('--dist-url', default='env://', type=str, 
                        help='url used to set up the distributed training' )
    parser.add_argument('-dw', '--disable-wandb', dest="disable_wandb", action='store_true', 
                        help='whether to use the wandb' )
    parser.add_argument('--sync-bn', dest='sync_bn', action='store_true',
                        help='use sync batch norm')
    parser.add_argument('--amp', action='store_true', 
                        help='use automatic mixed precision')
    parser.add_argument('--seed', default=0, type=int , 
                        help='golabl seed for training')
    parser.add_argument('--project-dir', dest='project_dir', default=None, type=str, 
                        help='save resutls to custom dir instead of `outputs` directory, --project-dir will be named if not already present')

    args = vars(parser.parse_args())
    return args


def criterion(outputs, targets):
    loss_classifier = torch.nn.functional.cross_entropy(outputs['logits'], targets['labels'])
    loss_box_reg = torch.nn.functional.l1_loss(outputs['boxes'], targets['boxes'])
    loss_objectness = torch.nn.functional.binary_cross_entropy_with_logits(outputs['objectness'], targets['objectness'])
    loss_rpn_box_reg = torch.nn.functional.smooth_l1_loss(outputs['rpn_box_reg'], targets['rpn_box_reg'])
    
    return {
        'loss_classifier': loss_classifier,
        'loss_box_reg': loss_box_reg,
        'loss_objectness': loss_objectness,
        'loss_rpn_box_reg': loss_rpn_box_reg
    }

def calculate_losses(outputs, targets):
    # Initialize the losses
    loss_classifier = 0
    loss_box_reg = 0
    loss_objectness = 0
    loss_rpn_box_reg = 0

    for output, target in zip(outputs, targets):
        # Extract boxes, labels, and scores from outputs
        pred_boxes = output['boxes']
        pred_labels = output['labels']
        #pred_scores = output['scores']

        # Extract ground truth boxes and labels from targets
        gt_boxes = target['boxes']
        gt_labels = target['labels']

        # Calculate the classification loss
        cls_loss = F.cross_entropy(pred_labels, gt_labels)

        # Calculate the bounding box regression loss
        reg_loss = F.l1_loss(pred_boxes, gt_boxes)

        # Assume objectness and RPN box regression losses are calculated similarly
        # For simplicity, using 0 for these losses in this example
        obj_loss = torch.tensor(0.0, device=pred_boxes.device)
        rpn_reg_loss = torch.tensor(0.0, device=pred_boxes.device)

        # Accumulate the losses
        loss_classifier += cls_loss
        loss_box_reg += reg_loss
        loss_objectness += obj_loss
        loss_rpn_box_reg += rpn_reg_loss

    return {
        'loss_classifier': loss_classifier,
        'loss_box_reg': loss_box_reg,
        'loss_objectness': loss_objectness,
        'loss_rpn_box_reg': loss_rpn_box_reg
    }


def validate_one_epoch(model, val_loader, device, epoch, print_freq):
    model.eval()  # Ensure model is in evaluation mode
    
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter("lr", utils.SmoothedValue(window_size=1, fmt="{value:.6f}"))
    header = f"Epoch: [{epoch}]"

    # Lists to store batch losses
    batch_loss_list = []
    batch_loss_cls_list = []
    batch_loss_box_reg_list = []
    batch_loss_objectness_list = []
    batch_loss_rpn_list = []

    with torch.no_grad():  # Disable gradient calculation
        for images, targets in metric_logger.log_every(val_loader, print_freq, header):
            images = list(image.to(device) for image in images)
            targets = [{k: v.to(device).to(torch.int64) for k, v in t.items()} for t in targets]

            # Forward pass
            outputs = model(images)

            # Manually calculate the losses using the same criteria as in training
            # loss_dict = criterion(outputs, targets)
            loss_dict = calculate_losses(outputs, targets)

            # Compute total loss
            losses = sum(loss for loss in loss_dict.values())

            # Reduce losses over all GPUs for logging purposes
            loss_dict_reduced = utils.reduce_dict(loss_dict)
            losses_reduced = sum(loss for loss in loss_dict_reduced.values())

            loss_value = losses_reduced.item()

            metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
            metric_logger.update(lr=0)  # Learning rate is not updated during validation

            batch_loss_list.append(loss_value)
            batch_loss_cls_list.append(loss_dict_reduced.get('loss_classifier', torch.tensor(0)).detach().cpu())
            batch_loss_box_reg_list.append(loss_dict_reduced.get('loss_box_reg', torch.tensor(0)).detach().cpu())
            batch_loss_objectness_list.append(loss_dict_reduced.get('loss_objectness', torch.tensor(0)).detach().cpu())
            batch_loss_rpn_list.append(loss_dict_reduced.get('loss_rpn_box_reg', torch.tensor(0)).detach().cpu())

    return (metric_logger, 
            batch_loss_list, batch_loss_cls_list, batch_loss_box_reg_list, 
            batch_loss_objectness_list, batch_loss_rpn_list
            )



def create_log_csv(log_dir):
    cols = ['epoch', 
            'train_map', 'train_map_05',
            'train loss', 'train cls loss','train box reg loss','train obj loss', 'train rpn loss'
            'val_map', 'val_map_05',
            'val loss', 'val cls loss','val box reg loss','val obj loss', 'val rpn loss'
            ]
    results_csv = pd.DataFrame(columns=cols)
    results_csv.to_csv(os.path.join(log_dir, 'results.csv'), index=False)

def csv_log(log_dir, stats_train, stats_val, epoch,
            train_loss_list, loss_cls_list, loss_box_reg_list, loss_objectness_list, loss_rpn_list,
            val_loss_list, loss_cls_list_val, loss_box_reg_list_val, loss_objectness_list_val, loss_rpn_list_val):
    if epoch+1 == 1:
        create_log_csv(log_dir) 
    
    df = pd.DataFrame(
        {
            'epoch': int(epoch+1),
            'train_map_05': [float(stats_train[0])],
            'train_map': [float(stats_train[1])],
            'train loss': train_loss_list[-1],
            'train cls loss': loss_cls_list[-1],
            'train box reg loss': loss_box_reg_list[-1],
            'train obj loss': loss_objectness_list[-1],
            'train rpn loss': loss_rpn_list[-1],

            'val_map_05': [float(stats_val[0])],
            'val_map': [float(stats_val[1])],
            'val loss': val_loss_list[-1],
            'val cls loss': loss_cls_list_val[-1],
            'val box reg loss': loss_box_reg_list_val[-1],
            'val obj loss': loss_objectness_list_val[-1],
            'val rpn loss': loss_rpn_list_val[-1]
        }
    )
    df.to_csv(os.path.join(log_dir, 'results.csv'), mode='a', index=False, header=False)



def main(args):
    # Initialize distributed mode.
    utils.init_distributed_mode(args)

    # Initialize W&B with project name.
    if not args['disable_wandb']:
        wandb_init(name=args['name'])
    # Load the data configurations
    with open(args['data']) as file:
        data_configs = yaml.safe_load(file)

    init_seeds(args['seed'] + 1 + RANK, deterministic=True)
    
    # Settings/parameters/constants.
    TRAIN_DIR_IMAGES = os.path.normpath(data_configs['TRAIN_DIR_IMAGES'])
    TRAIN_DIR_LABELS = os.path.normpath(data_configs['TRAIN_DIR_LABELS'])
    VALID_DIR_IMAGES = os.path.normpath(data_configs['VALID_DIR_IMAGES'])
    VALID_DIR_LABELS = os.path.normpath(data_configs['VALID_DIR_LABELS'])
    CLASSES = data_configs['CLASSES']
    NUM_CLASSES = data_configs['NC']
    NUM_WORKERS = args['workers']
    DEVICE = torch.device(args['device'])
    print("device",DEVICE)
    NUM_EPOCHS = args['epochs']
    SAVE_VALID_PREDICTIONS = data_configs['SAVE_VALID_PREDICTION_IMAGES']
    BATCH_SIZE = args['batch']
    VISUALIZE_TRANSFORMED_IMAGES = args['vis_transformed']
    OUT_DIR = set_training_dir(args['name'], args['project_dir'])
    COLORS = np.random.uniform(0, 1, size=(len(CLASSES), 3))
    SCALER = torch.cuda.amp.GradScaler() if args['amp'] else None
    # Set logging file.
    set_log(OUT_DIR)
    writer = set_summary_writer(OUT_DIR)

    yaml_save(file_path=os.path.join(OUT_DIR, 'opt.yaml'), data=args)

    # Model configurations
    IMAGE_SIZE = args['imgsz']
    
    train_dataset = create_train_dataset(
        TRAIN_DIR_IMAGES, 
        TRAIN_DIR_LABELS,
        IMAGE_SIZE, 
        CLASSES,
        use_train_aug=args['use_train_aug'],
        mosaic=args['mosaic'],
        square_training=args['square_training']
    )
    valid_dataset = create_valid_dataset(
        VALID_DIR_IMAGES, 
        VALID_DIR_LABELS, 
        IMAGE_SIZE, 
        CLASSES,
        square_training=args['square_training']
    )
    print('Creating data loaders')
    if args['distributed']:
        train_sampler = distributed.DistributedSampler(
            train_dataset
        )
        valid_sampler = distributed.DistributedSampler(
            valid_dataset, shuffle=False
        )
    else:
        train_sampler = RandomSampler(train_dataset)
        valid_sampler = SequentialSampler(valid_dataset)

    train_loader = create_train_loader(train_dataset, BATCH_SIZE, NUM_WORKERS, batch_sampler=train_sampler)
    valid_loader = create_valid_loader(valid_dataset, BATCH_SIZE, NUM_WORKERS, batch_sampler=valid_sampler)
    print(f"Number of training samples: {len(train_dataset)}")
    print(f"Number of validation samples: {len(valid_dataset)}\n")

    if VISUALIZE_TRANSFORMED_IMAGES:
        show_tranformed_image(train_loader, DEVICE, CLASSES, COLORS)

    # Initialize the Averager class.
    train_loss_hist = Averager()
    train_loss_list = []
    loss_cls_list = []
    loss_box_reg_list = []
    loss_objectness_list = []
    loss_rpn_list = []
    train_loss_list_epoch = []
    train_map_05 = []
    train_map = []

    val_loss_hist = Averager()
    val_loss_list = []
    loss_cls_list_val = []
    loss_box_reg_list_val = []
    loss_objectness_list_val = []
    loss_rpn_list_val = []
    val_loss_list_epoch = []
    val_map_05 = []
    val_map = []

    start_epochs = 0
    

    if args['weights'] is None:
        print('Building model from scratch...')
        build_model = create_model[args['model']]
        model = build_model(num_classes=NUM_CLASSES, pretrained=True)

    # Load pretrained weights if path is provided.
    if args['weights'] is not None:
        print('Loading pretrained weights...')
        
        # Load the pretrained checkpoint.
        checkpoint = torch.load(args['weights'], map_location=DEVICE) 
        keys = list(checkpoint['model_state_dict'].keys())
        ckpt_state_dict = checkpoint['model_state_dict']
        # Get the number of classes from the loaded checkpoint.
        old_classes = ckpt_state_dict['roi_heads.box_predictor.cls_score.weight'].shape[0]

        # Build the new model with number of classes same as checkpoint.
        build_model = create_model[args['model']]
        model = build_model(num_classes=old_classes)
        # Load weights.
        model.load_state_dict(ckpt_state_dict)

        # Change output features for class predictor and box predictor
        # according to current dataset classes.
        in_features = model.roi_heads.box_predictor.cls_score.in_features
        model.roi_heads.box_predictor.cls_score = torch.nn.Linear(
            in_features=in_features, out_features=NUM_CLASSES, bias=True
        )
        model.roi_heads.box_predictor.bbox_pred = torch.nn.Linear(
            in_features=in_features, out_features=NUM_CLASSES*4, bias=True
        )

        if args['resume_training']:
            print('RESUMING TRAINING...')
            # Update the starting epochs, the batch-wise loss list, 
            # and the epoch-wise loss list.
            if checkpoint['epoch']:
                start_epochs = checkpoint['epoch']
                print(f"Resuming from epoch {start_epochs}...")
            if checkpoint['train_loss_list']:
                print('Loading previous batch wise loss list...')
                train_loss_list = checkpoint['train_loss_list']
            if checkpoint['train_loss_list_epoch']:
                print('Loading previous epoch wise loss list...')
                train_loss_list_epoch = checkpoint['train_loss_list_epoch']
            if checkpoint['val_map']:
                print('Loading previous mAP list')
                val_map = checkpoint['val_map']
            if checkpoint['val_map_05']:
                val_map_05 = checkpoint['val_map_05']

    # Make the model transform's `min_size` same as `imgsz` argument. 
    model.transform.min_size = (args['imgsz'], )
    model = model.to(DEVICE)
    if args['sync_bn'] and args['distributed']:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    if args['distributed']:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args['gpu']]
        )
    try:
        torchinfo.summary(
            model, 
            device=DEVICE, 
            input_size=(BATCH_SIZE, 3, IMAGE_SIZE, IMAGE_SIZE),
            row_settings=["var_names"],
            col_names=("input_size", "output_size", "num_params") 
        )
    except:
        print(model)
    # Total parameters and trainable parameters.
    total_params = sum(p.numel() for p in model.parameters())
    print(f"{total_params:,} total parameters.")
    total_trainable_params = sum(
        p.numel() for p in model.parameters() if p.requires_grad)
    print(f"{total_trainable_params:,} training parameters.")
    # Get the model parameters.
    params = [p for p in model.parameters() if p.requires_grad]
    # Define the optimizer.
    optimizer = torch.optim.SGD(params, lr=args['lr'], momentum=0.9, nesterov=True)
    # optimizer = torch.optim.AdamW(params, lr=0.0001, weight_decay=0.0005)
    if args['resume_training']: 
        # LOAD THE OPTIMIZER STATE DICTIONARY FROM THE CHECKPOINT.
        print('Loading optimizer state dictionary...')
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    if args['cosine_annealing']:
        # LR will be zero as we approach `steps` number of epochs each time.
        # If `steps = 5`, LR will slowly reduce to zero every 5 epochs.
        steps = NUM_EPOCHS + 10
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, 
            T_0=steps,
            T_mult=1,
            verbose=False
        )
    else:
        scheduler = None

    save_best_model = SaveBestModel()

    for epoch in range(start_epochs, NUM_EPOCHS):
        train_loss_hist.reset()

        _, batch_loss_list, \
            batch_loss_cls_list, \
            batch_loss_box_reg_list, \
            batch_loss_objectness_list, \
            batch_loss_rpn_list = train_one_epoch(
                model, optimizer, train_loader, 
                DEVICE, epoch, train_loss_hist,
                print_freq=100, scheduler=scheduler, scaler=SCALER
                )

        _, batch_loss_list_val, \
            batch_loss_cls_list_val, \
            batch_loss_box_reg_list_val, \
            batch_loss_objectness_list_val, \
            batch_loss_rpn_list_val = validate_one_epoch(
                model, valid_loader, 
                DEVICE, epoch,
                print_freq=100
                )


        stats_train, _ = evaluate(
            model, 
            train_loader, 
            device=DEVICE, save_valid_preds=SAVE_VALID_PREDICTIONS,
            out_dir=OUT_DIR, classes=CLASSES,colors=COLORS
        )

        stats_val, val_pred_image = evaluate(
            model, 
            valid_loader, 
            device=DEVICE, save_valid_preds=SAVE_VALID_PREDICTIONS,
            out_dir=OUT_DIR, classes=CLASSES, colors=COLORS
        )

        # Append the current epoch's batch-wise losses to the `train_loss_list`.
        train_loss_list.extend(batch_loss_list)
        loss_cls_list.append(np.mean(np.array(batch_loss_cls_list,)))
        loss_box_reg_list.append(np.mean(np.array(batch_loss_box_reg_list)))
        loss_objectness_list.append(np.mean(np.array(batch_loss_objectness_list)))
        loss_rpn_list.append(np.mean(np.array(batch_loss_rpn_list)))

        # Append curent epoch's average loss to `train_loss_list_epoch`.
        train_loss_list_epoch.append(train_loss_hist.value)
        train_map_05.append(stats_train[1])
        train_map.append(stats_train[0])


        # Append the current epoch's batch-wise losses to the `train_loss_list`.
        val_loss_list.extend(batch_loss_list_val)
        loss_cls_list_val.append(np.mean(np.array(batch_loss_cls_list_val,)))
        loss_box_reg_list_val.append(np.mean(np.array(batch_loss_box_reg_list_val)))
        loss_objectness_list_val.append(np.mean(np.array(batch_loss_objectness_list_val)))
        loss_rpn_list_val.append(np.mean(np.array(batch_loss_rpn_list_val)))

        # Append curent epoch's average loss to `train_loss_list_epoch`.
        val_loss_list_epoch.append(val_loss_hist.value)
        val_map_05.append(stats_val[1])
        val_map.append(stats_val[0])


        # Save loss plot for batch-wise list.
        save_loss_plot(OUT_DIR, train_loss_list, save_name='train_loss')
        save_loss_plot(OUT_DIR, val_loss_list, save_name='val_loss')
        # Save loss plot for epoch-wise list.
        save_loss_plot(OUT_DIR, train_loss_list_epoch, 'epochs', 'train loss',
                       save_name='train_loss_epoch')
        # Save all the training loss plots.
        save_loss_plot(OUT_DIR, loss_cls_list, 'epochs', 'loss cls', 
                       save_name='train_loss_cls')
        save_loss_plot(OUT_DIR, loss_box_reg_list, 'epochs', 'loss bbox reg',
                       save_name='train_loss_bbox_reg')
        save_loss_plot(OUT_DIR, loss_objectness_list, 'epochs', 'loss obj',
                       save_name='train_loss_obj')
        save_loss_plot(OUT_DIR,loss_rpn_list,'epochs','loss rpn bbox',
                       save_name='train_loss_rpn_bbox')

        # Save mAP plots.
        save_mAP(OUT_DIR, train_map_05, val_map_05)
        save_mAP(OUT_DIR, train_map, val_map)

        # Save batch-wise train loss plot using TensorBoard. Better not to use it
        # as it increases the TensorBoard log sizes by a good extent (in 100s of MBs).
        # tensorboard_loss_log('Train loss', np.array(train_loss_list), writer)

        # Save epoch-wise train loss plot using TensorBoard.
        tensorboard_loss_log('Train loss', np.array(train_loss_list_epoch), writer,epoch)

        # Save mAP plot using TensorBoard.
        tensorboard_map_log(name='mAP', val_map_05=np.array(val_map_05), val_map=np.array(val_map), writer=writer, epoch=epoch)

        coco_log(OUT_DIR, stats_train)
        csv_log(OUT_DIR, stats_train, stats_val, epoch,
                train_loss_list, loss_cls_list, loss_box_reg_list, loss_objectness_list, loss_rpn_list,
                val_loss_list, loss_cls_list_val, loss_box_reg_list_val, loss_objectness_list_val, loss_rpn_list_val)

        # WandB logging.
        if not args['disable_wandb']:
            wandb_log(train_loss_hist.value, batch_loss_list, loss_cls_list, loss_box_reg_list, loss_objectness_list, loss_rpn_list,
                      stats_train[1], stats_train[0], val_pred_image, IMAGE_SIZE)

        # Save the current epoch model state. This can be used 
        # to resume training. It saves model state dict, number of
        # epochs trained for, optimizer state dict, and loss function.
        save_model(epoch, model, optimizer, 
                   train_loss_list, train_loss_list_epoch,
                   val_map, val_map_05,
                   OUT_DIR, data_configs, args['model']
                   )
        # Save the model dictionary only for the current epoch.
        save_model_state(model, OUT_DIR, data_configs, args['model'])
        # Save best model if the current mAP @0.5:0.95 IoU is
        # greater than the last hightest.
        save_best_model(model, val_map[-1], epoch, OUT_DIR, data_configs, args['model'])
    
    # Save models to Weights&Biases.
    if not args['disable_wandb']:
        wandb_save_model(OUT_DIR)


if __name__ == '__main__':
    args = parse_opt()
    main(args)

