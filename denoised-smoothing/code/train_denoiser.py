# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# File for training denoisers with at most one classifier attached to

from architectures import DENOISERS_ARCHITECTURES, get_architecture, IMAGENET_CLASSIFIERS
from datasets import get_dataset, DATASETS
#from test_denoiser import test, test_with_classifier
from torch.nn import MSELoss, CrossEntropyLoss
from torch.optim import SGD, Optimizer, Adam
from torch.optim.lr_scheduler import StepLR, MultiStepLR
from torch.utils.data import DataLoader
from torchvision.transforms import ToPILImage
from train_utils import AverageMeter, accuracy, init_logfile, log, copy_code, requires_grad_

import argparse
import datetime
import numpy as np
import os
import time
import torch
import torchvision

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('--dataset', type=str, choices=DATASETS)
parser.add_argument('--arch', type=str, choices=DENOISERS_ARCHITECTURES)
parser.add_argument('--outdir', type=str, help='folder to save denoiser and training log)')
parser.add_argument('--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--batch', default=256, type=int, metavar='N',
                    help='batchsize (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate', dest='lr')
parser.add_argument('--lr_step_size', type=int, default=30,
                    help='How often to decrease learning by gamma.')
parser.add_argument('--gamma', type=float, default=0.1,
                    help='LR is multiplied by gamma on schedule.')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--gpu', default=None, type=str,
                    help='id(s) for CUDA_VISIBLE_DEVICES')
parser.add_argument('--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--noise_sd', default=0.0, type=float,
                    help="standard deviation of noise distribution for data augmentation")
parser.add_argument('--noise_type', default="normal", choices=["normal","uniform","split","split_derandomized"], type=str,
                    help="type of noise")
parser.add_argument('--objective', default='denoising', type=str,
                    help="the objective that is used to train the denoiser",
                    choices=['denoising', 'classification', 'stability'])
parser.add_argument('--classifier', default='', type=str,
                    help='path to the classifier used with the `classificaiton`'
                     'or `stability` objectives of the denoiser.')
parser.add_argument('--pretrained-denoiser', default='', type=str,
                    help='path to a pretrained denoiser')
parser.add_argument('--optimizer', default='Adam', type=str,
                    help='SGD, Adam, or Adam then SGD', choices=['SGD', 'Adam','AdamThenSGD'])
parser.add_argument('--start-sgd-epoch', default=50, type=int,
                    help='[Relevent only to AdamThenSGD.] Epoch at which adam switches to SGD')
parser.add_argument('--start-sgd-lr', default=1e-3, type=float,
                    help='[Relevent only to AdamThenSGD.] LR at which SGD starts after Adam')
parser.add_argument('--resume', action='store_true',
                    help='if true, tries to resume training from an existing checkpoint')
parser.add_argument('--azure_datastore_path', type=str, default='',
                    help='Path to imagenet on azure')
parser.add_argument('--philly_imagenet_path', type=str, default='',
                    help='Path to imagenet on philly')
parser.add_argument("--seed", default=0, type=int)

args = parser.parse_args()

if args.azure_datastore_path:
    os.environ['IMAGENET_DIR_AZURE'] = os.path.join(args.azure_datastore_path, 'datasets/imagenet_zipped')
if args.philly_imagenet_path:
    os.environ['IMAGENET_DIR_PHILLY'] = os.path.join(args.philly_imagenet_path, './')

torch.manual_seed(0)
torch.cuda.manual_seed_all(0)

toPilImage = ToPILImage()


lambd = args.noise_sd * (3**0.5)
if (args.noise_type == "split_derandomized"):
    if (args.dataset == 'imagenet'):
        dim = 3*224*224
    else:
        dim = 3 * 32 *32
    if (lambd < 0.5):
        raise NotImplementedError()
    levels = int(2*lambd*255)
    rs = np.random.RandomState(seed=args.seed)
    template = torch.tensor(rs.randint(levels, size=dim)).to('cuda').reshape(1,dim)


def main():
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # Copy code to output directory
    copy_code(args.outdir)
    
    train_dataset = get_dataset(args.dataset, 'train')
    test_dataset = get_dataset(args.dataset, 'test')
    pin_memory = (args.dataset == "imagenet")
    train_loader = DataLoader(train_dataset, shuffle=True, batch_size=args.batch,
                              num_workers=args.workers, pin_memory=pin_memory)
    test_loader = DataLoader(test_dataset, shuffle=False, batch_size=args.batch,
                             num_workers=args.workers, pin_memory=pin_memory)
    ## This is used to test the performance of the denoiser attached to a cifar10 classifier
    cifar10_test_loader = DataLoader(get_dataset('cifar10', 'test'), shuffle=False, batch_size=args.batch,
                             num_workers=args.workers, pin_memory=pin_memory)

    if args.pretrained_denoiser:
        checkpoint = torch.load(args.pretrained_denoiser)
        assert checkpoint['arch'] == args.arch
        denoiser = get_architecture(checkpoint['arch'], args.dataset)
        denoiser.load_state_dict(checkpoint['state_dict'])
    else:
        denoiser = get_architecture(args.arch, args.dataset)

    if args.optimizer == 'Adam':
        optimizer = Adam(denoiser.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    elif args.optimizer == 'SGD':
        optimizer = SGD(denoiser.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    elif args.optimizer == 'AdamThenSGD':
        optimizer = Adam(denoiser.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = StepLR(optimizer, step_size=args.lr_step_size, gamma=args.gamma)

    starting_epoch = 0
    logfilename = os.path.join(args.outdir, 'log.txt')

    ## Resume from checkpoint if exists and if resume flag is True
    denoiser_path = os.path.join(args.outdir, 'checkpoint.pth.tar')
    if args.resume and os.path.isfile(denoiser_path):
        print("=> loading checkpoint '{}'".format(denoiser_path))
        checkpoint = torch.load(denoiser_path,
                                map_location=lambda storage, loc: storage)
        assert checkpoint['arch'] == args.arch
        starting_epoch = checkpoint['epoch']
        denoiser.load_state_dict(checkpoint['state_dict'])
        if starting_epoch >= args.start_sgd_epoch and args.optimizer == 'AdamThenSGD ': # Do adam for few steps thaen continue SGD
            print("-->[Switching from Adam to SGD.]")
            args.lr = args.start_sgd_lr
            optimizer = SGD(denoiser.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
            scheduler = StepLR(optimizer, step_size=args.lr_step_size, gamma=args.gamma)
        
        optimizer.load_state_dict(checkpoint['optimizer'])
        print("=> loaded checkpoint '{}' (epoch {})"
                        .format(denoiser_path, checkpoint['epoch']))
    else:
        if args.resume: print("=> no checkpoint found at '{}'".format(args.outdir))
        init_logfile(logfilename, "epoch\ttime\tlr\ttrainloss\ttestloss\ttestAcc")


    if args.objective == 'denoising':
        criterion = MSELoss(size_average=None, reduce=None, reduction = 'mean').cuda()
        best_loss = 1e6

    elif args.objective in ['classification', 'stability']:
        assert args.classifier != '', "Please specify a path to the classifier you want to attach the denoiser to."

        if args.classifier in IMAGENET_CLASSIFIERS:
            assert args.dataset == 'imagenet'
            # loading pretrained imagenet architectures
            clf = get_architecture(args.classifier, args.dataset, pytorch_pretrained=True)
        else:
            checkpoint = torch.load(args.classifier)
            clf = get_architecture(checkpoint['arch'], 'cifar10')
            clf.load_state_dict(checkpoint['state_dict'])
        clf.cuda().eval()
        requires_grad_(clf, False)
        criterion = CrossEntropyLoss(size_average=None, reduce=None, reduction = 'mean').cuda()
        best_acc = 0

    for epoch in range(starting_epoch, args.epochs):
        before = time.time()
        if args.objective == 'denoising':
            train_loss = train(train_loader, denoiser, criterion, optimizer, epoch, args.noise_sd)
            test_loss = test(test_loader, denoiser, criterion, args.noise_sd, args.print_freq, args.outdir)
            test_acc = 'NA'
        elif args.objective in ['classification', 'stability']:
            train_loss = train(train_loader, denoiser, criterion, optimizer, epoch, args.noise_sd, clf)
            if args.dataset == 'imagenet': 
                test_loss, test_acc = test_with_classifier(test_loader, denoiser, criterion, args.noise_sd, args.print_freq, clf)
            else:
                # This is needed so that cifar10 denoisers trained using imagenet32 are still evaluated on the cifar10 testset
                test_loss, test_acc = test_with_classifier(cifar10_test_loader, denoiser, criterion, args.noise_sd, args.print_freq, clf)

        after = time.time()

        log(logfilename, "{}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{:.3}".format(
            epoch, after - before,
            args.lr, train_loss, test_loss, test_acc))

        scheduler.step(epoch)
        args.lr = scheduler.get_lr()[0]

        # Switch from Adam to SGD
        if epoch == args.start_sgd_epoch and args.optimizer == 'AdamThenSGD ': # Do adam for few steps thaen continue SGD
            print("-->[Switching from Adam to SGD.]")
            args.lr = args.start_sgd_lr
            optimizer = SGD(denoiser.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
            scheduler = StepLR(optimizer, step_size=args.lr_step_size, gamma=args.gamma)

        torch.save({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': denoiser.state_dict(),
            'optimizer': optimizer.state_dict(),
        }, os.path.join(args.outdir, 'checkpoint.pth.tar'))

        if args.objective == 'denoising' and test_loss < best_loss:
            best_loss = test_loss
        elif args.objective in ['classification', 'stability'] and test_acc > best_acc:
            best_acc = test_acc
        else:
            continue

        torch.save({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': denoiser.state_dict(),
            'optimizer': optimizer.state_dict(),
        }, os.path.join(args.outdir, 'best.pth.tar'))



def train(loader: DataLoader, denoiser: torch.nn.Module, criterion, optimizer: Optimizer, epoch: int, noise_sd: float, classifier: torch.nn.Module=None):
    """
    Function for training denoiser for one epoch
        :param loader:DataLoader: training dataloader
        :param denoiser:torch.nn.Module: the denoiser being trained
        :param criterion: loss function
        :param optimizer:Optimizer: optimizer used during trainined
        :param epoch:int: the current epoch (for logging)
        :param noise_sd:float: the std-dev of the Guassian noise perturbation of the input
        :param classifier:torch.nn.Module=None: a ``freezed'' classifier attached to the denoiser 
                                                (required classifciation/stability objectives), None for denoising objective 
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    end = time.time()
    # switch to train mode
    denoiser.train()
    if classifier:
        classifier.eval()

    for i, (inputs, targets) in enumerate(loader):
        # measure data loading time
        data_time.update(time.time() - end)

        inputs = inputs.cuda()
        targets = targets.cuda()
        # augment inputs with noise
        if (args.noise_type == "normal"):
            noise = torch.randn_like(inputs, device='cuda') * noise_sd
            outputs = denoiser(inputs + noise)
        elif (args.noise_type == "uniform"):
            noise = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd
            outputs = denoiser(inputs + noise)
        elif (args.noise_type == "split"):
            split_values = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd +0.5
            gt = (inputs > split_values).float()
            clipped_splits = torch.clamp(split_values,min=0,max =1)
            noised = (gt + clipped_splits)/2
            outputs = denoiser(noised)
        elif (args.noise_type == "split_derandomized"):
            offset = torch.randint(levels,(inputs.shape[0],1), device='cuda')
            split_values = (((template + offset) % levels + 0.5)/255.0).reshape(inputs.shape)
            gt = (inputs > split_values).float()
            clipped_splits = torch.clamp(split_values,min=0,max =1)
            noised = (gt + clipped_splits)/2
            outputs = denoiser(noised)
        # compute output
        if classifier:
            outputs = classifier(outputs)
        
        if isinstance(criterion, MSELoss):
            loss = criterion(outputs, inputs)
        elif isinstance(criterion, CrossEntropyLoss):
            if args.objective == 'stability':
                with torch.no_grad():
                    targets = classifier(inputs)
                    targets = targets.argmax(1).detach().clone()
            loss = criterion(outputs, targets)

        # record loss
        losses.update(loss.item(), inputs.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                epoch, i, len(loader), batch_time=batch_time,
                data_time=data_time, loss=losses))

    return losses.avg

def test(loader: DataLoader, model: torch.nn.Module, criterion, noise_sd: float, print_freq: int, outdir: str):
    """
    A function to test the denoising performance of a denoiser (i.e. MSE objective)
        :param loader:DataLoader: test dataloader
        :param model:torch.nn.Module: the denoiser
        :param criterion: the loss function
        :param noise_sd:float: the std-dev of the Guassian noise perturbation of the input
        :param print_freq:int: 
        :param outdir:str: the output directory where sample denoised images are saved.
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    end = time.time()

    # switch to eval mode
    model.eval()

    with torch.no_grad():
        for i, (inputs, targets) in enumerate(loader):
            # measure data loading time
            data_time.update(time.time() - end)

            inputs = inputs.cuda()
            targets = targets.cuda()

            # augment inputs with noise
            if (args.noise_type == "normal"):
                noise = torch.randn_like(inputs, device='cuda') * noise_sd
                outputs = model(inputs + noise)
            elif (args.noise_type == "uniform"):
                noise = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd
                outputs = model(inputs + noise)
            elif (args.noise_type == "split"):
                split_values = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd +0.5
                gt = (inputs > split_values).float()
                clipped_splits = torch.clamp(split_values,min=0,max =1)
                noised = (gt + clipped_splits)/2
                outputs = model(noised)
            elif (args.noise_type == "split_derandomized"):
                offset = torch.randint(levels,(inputs.shape[0],1), device='cuda')
                split_values = (((template + offset) % levels + 0.5)/255.0).reshape(inputs.shape)
                gt = (inputs > split_values).float()
                clipped_splits = torch.clamp(split_values,min=0,max =1)
                noised = (gt + clipped_splits)/2
                outputs = model(noised)

            loss = criterion(outputs, inputs)

            # record loss
            losses.update(loss.item(), inputs.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                    i, len(loader), batch_time=batch_time,
                    data_time=data_time, loss=losses))

        pil = toPilImage(inputs[0].cpu())
        image_path = os.path.join(outdir, 'clean.png')
        pil.save(image_path)

        pil = toPilImage(outputs[0].cpu())
        image_path = os.path.join(outdir, 'denoised.png')
        pil.save(image_path)

        return losses.avg


def test_with_classifier(loader: DataLoader, denoiser: torch.nn.Module, criterion, noise_sd: float, print_freq: int, classifier: torch.nn.Module):
    """
    A function to test the classification performance of a denoiser when attached to a given classifier
        :param loader:DataLoader: test dataloader
        :param denoiser:torch.nn.Module: the denoiser 
        :param criterion: the loss function (e.g. CE)
        :param noise_sd:float: the std-dev of the Guassian noise perturbation of the input
        :param print_freq:int: the frequency of logging
        :param classifier:torch.nn.Module: the classifier to which the denoiser is attached
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    # switch to eval mode
    classifier.eval()
    if denoiser:
        denoiser.eval()

    with torch.no_grad():
        for i, (inputs, targets) in enumerate(loader):
            # measure data loading time
            data_time.update(time.time() - end)

            inputs = inputs.cuda()
            targets = targets.cuda()

            # augment inputs with noise

            if (args.noise_type == "normal"):
                noise = torch.randn_like(inputs, device='cuda') * noise_sd
                inputs = inputs + noise
            elif (args.noise_type == "uniform"):
                noise = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd
                inputs = inputs + noise
            elif (args.noise_type == "split"):
                split_values = (torch.rand_like(inputs, device='cuda')  - 0.5) *2 *lambd +0.5
                gt = (inputs > split_values).float()
                clipped_splits = torch.clamp(split_values,min=0,max =1)
                inputs = (gt + clipped_splits)/2
            elif (args.noise_type == "split_derandomized"):
                offset = torch.randint(levels,(inputs.shape[0],1), device='cuda')
                split_values = (((template + offset) % levels + 0.5)/255.0).reshape(inputs.shape)
                gt = (inputs > split_values).float()
                clipped_splits = torch.clamp(split_values,min=0,max =1)
                inputs = (gt + clipped_splits)/2

            if denoiser is not None:
                inputs = denoiser(inputs)
            # compute output
            outputs = classifier(inputs)
            loss = criterion(outputs, targets)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.size(0))
            top1.update(acc1.item(), inputs.size(0))
            top5.update(acc5.item(), inputs.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                    i, len(loader), batch_time=batch_time,
                    data_time=data_time, loss=losses, top1=top1, top5=top5))

        return (losses.avg, top1.avg)

if __name__ == "__main__":
    main()
