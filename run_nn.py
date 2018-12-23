import os
import sys
import argparse
import re
import glob
import datetime
import time
import numpy as np
import torch
import nnModules
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim.lr_scheduler import MultiStepLR, LambdaLR, StepLR
from dataset_torch_3 import DenoisingDataset
from lib import pytorch_ssim
from random import randint
from torchvision import models

# Params
parser = argparse.ArgumentParser(description='PyTorch Denoising network trainer')
parser.add_argument('--model', default='UNet', type=str, help='Model type (UNet, DnCNN, RedCNN)')
parser.add_argument('--batch_size', default=32, type=int, help='batch size')
parser.add_argument('--train_data', default='datasets/train/ds_128_96', type=str, help='Path to the pre-cropped training data (default: '+'datasets/train/dataset_128_96'+')')
parser.add_argument('--epoch', default=32768, type=int, help='Number of train epoches')
parser.add_argument('--time_limit', default=172800, type=int, help='Time limit in seconds')
parser.add_argument('--lr', default=5e-4, type=float, help='Initial learning rate for Adam')
parser.add_argument('--expname', type=str, help='Experiment name used to save and/or load results and models (default autogenerated from time+CLI)')
parser.add_argument('--resume', action='store_true', help='Look for an experiment with the same parameters and continue (to force continuing an experiment with different parameters use --expname instead)')
parser.add_argument('--result_dir', default='results/train', type=str, help='Directory where results are stored (default: results/train)')
parser.add_argument('--models_dir', default='models', type=str, help='Directory where models are saved/loaded (default: models)')
parser.add_argument('--depth', default=22, type=int, help='Number of layers (default: 22)')
parser.add_argument('--cuda_device', default=0, type=int, help='Device number (default: 0, typically 0-3)')
parser.add_argument('--n_channels', default=128, type=int, help='Number of channels (default: 128)')
parser.add_argument('--find_noise', action='store_true', help='(DnCNN) Model noise if set, otherwise generate clean image')
parser.add_argument('--kernel_size', default=5, type=int, help='Kernel size')
parser.add_argument('--compressionmin', type=str, default=100, help='Minimum compression level ([1,100], default=100)')
parser.add_argument('--compressionmax', type=int, default=100, help='Maximum compression level ([1,100], default=100)')
parser.add_argument('--sigmamin', type=int, default=0, help='Minimum sigma value ([0,100], default=0)')
parser.add_argument('--sigmamax', type=int, default=0, help='Maximum sigma value ([0,100], default=0)')
parser.add_argument('--yisx', action='store_true', help='Use base ISO only if flag is set (useful to compare with sigmamax>0 artificial noise)')
parser.add_argument('--scheduler', default='plateau', type=str, help='Scheduler; adjusts learning rate. Options are plateau, multistep, StepLR, random. default: plateau (*.75 without patience)')
parser.add_argument('--lr_gamma', default=.75, type=float, help='Learning rate decrease rate for plateau, StepLR (default: 0.75)')
parser.add_argument('--lr_step_size', default=1, type=int, help='Step size for StepLR, plateau scheduler')
parser.add_argument('--lossf', default='SSIM', help='Loss function (SSIM or MSE)')
parser.add_argument('--test_reserve', nargs='*', help='Space separated list of image sets to be reserved for testing')
parser.add_argument('--relu', default='relu', help='ReLU function (relu, rrelu)')

args = parser.parse_args()


# memory eg:
# 1996: res48x48 bs27 = 1932/1996 5260s
# 11172 d22 res96
#   bs12 = 2335
#   bs57 = 8883
#   bs71 = 10813
#   bs72 = 10941
# python3 run_nn.py --batch_size 36 --cuda_device 1 --n_channels 128 --kernel_size 5 : 11071 MB
# python3 run_nn.py --model RedCNN --epoch 76 --cuda_device 3 --n_channels 128 --kernel_size 5 --batch_size 40 --depth 22: 11053 MB


batch_size = args.batch_size
cuda = torch.cuda.is_available()
torch.cuda.set_device(args.cuda_device)

def find_experiment():
    exp = None
    bname = ('_'.join(sys.argv).replace('/','-')).replace('_--resume','')
    for adir in os.listdir(args.models_dir):
        if adir[17:]==bname:
            exp = adir
    return exp


if args.expname:
    expname = args.expname
else:
    if args.resume:
        expname = find_experiment()
        if expname == None:
            sys.exit('Error: cannot resume experiment (404)')
    else:
        expname = datetime.datetime.now().isoformat()[:-10]+'_'+'_'.join(sys.argv).replace('/','-')


save_dir = os.path.join('models', expname)
res_dir = os.path.join(args.result_dir, expname)

def findLastCheckpoint(save_dir):
    file_list = glob.glob(os.path.join(save_dir, 'model_*.pth'))
    if file_list:
        epochs_exist = []
        for file_ in file_list:
            result = re.findall(".*model_(.*).pth.*", file_)
            epochs_exist.append(int(result[0]))
        initial_epoch = max(epochs_exist)
    else:
        initial_epoch = 0
    return initial_epoch


def log(*args, **kwargs):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S:"), *args, **kwargs)

if __name__ == '__main__':
    # Model
    print('===> Building model')
    if args.model == 'DnCNN':
        model = nnModules.DnCNN(depth=args.depth, n_channels=args.n_channels, find_noise=args.find_noise, kernel_size=args.kernel_size)
    elif args.model == 'RedCNN':
        model = nnModules.RedCNN(depth=args.depth, n_channels=args.n_channels, kernel_size=args.kernel_size, relu=args.relu)
    elif args.model == 'RedishCNN':
        model = nnModules.RedishCNN(depth=args.depth, n_channels=args.n_channels, kernel_size=args.kernel_size)
    elif args.model == 'UNet':
        if args.relu == 'relu':
            model = nnModules.UNet(3,3)
        # ugliness while I figure out memory issue
        else:
            model = nnModules.RRUNet(3,3)
    elif args.model == 'exp':
        model = models.resnet50()

    else:
        exit(args.model+' not implemented.')
    initial_epoch = findLastCheckpoint(save_dir=save_dir)  # load the last model in matconvnet style
    if initial_epoch > 0:
        print('resuming by loading epoch %03d' % initial_epoch)
        # model.load_state_dict(torch.load(os.path.join(save_dir, 'model_%03d.pth' % initial_epoch)))
        model = torch.load(os.path.join(save_dir, 'model_%03d.pth' % initial_epoch))
    elif args.model != 'DnCNN':
        model.apply(nnModules.init_weights)
    model.train()
    # Loss function
    #if args.lossf == 'MSSSIM':
    #    criterion = pytorch_msssim.MSSSIM(channel=3)
    #elif args.lossf == 'MSSSIMandMSE':
    #    criterion = pytorch_msssim.MSSSIMandMSE()
    if args.lossf == 'SSIM':
        criterion = pytorch_ssim.SSIM()
    elif args.lossf == 'MSE':
        criterion = torch.nn.MSELoss()
    else:
        exit('Error: requested loss function '+args.lossf+' has not been implemented.')
    if cuda:
        model = model.cuda()
        # device_ids = [0]
        # model = nn.DataParallel(model, device_ids=device_ids).cuda()
        criterion = criterion.cuda()
    # Dataset
    DDataset = DenoisingDataset(args.train_data, compressionmin=args.compressionmin, compressionmax=args.compressionmax, sigmamin=args.sigmamin, sigmamax=args.sigmamax, test_reserve=args.test_reserve, yisx=args.yisx)
    DLoader = DataLoader(dataset=DDataset, num_workers=8, drop_last=True, batch_size=batch_size, shuffle=True)
    loss_crop_lb = int((DDataset.cs-DDataset.ucs)/2)
    loss_crop_up = loss_crop_lb+DDataset.ucs
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    # Scheduler
    # broken: non-integer stop for randrange()
    if args.scheduler == 'random':
        def lrlambda(epoch, lr=args.lr):
            newlr = randint(1,int(.1/lr))/randint(1,1/int(lr))
            print(newlr)
            return newlr
        #lrlambda = lambda epoch, lr=args.lr: randint(1,int(.1/lr))/randint(1,1/int(lr))
        scheduler = LambdaLR(optimizer, lrlambda)
    elif args.scheduler == 'multistep':
        scheduler = MultiStepLR(optimizer, milestones=[args.epoch*.02, args.epoch*.06, args.epoch*.14, args.epoch*.30, args.epoch*.62, args.epoch*.78, args.epoch*.86], gamma=0.5)  # learning rates
    elif args.scheduler == 'StepLR':
        scheduler = StepLR(optimizer, step_size = args.lr_step_size, gamma = args.lr_gamma)
    else:
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=args.lr_step_size, verbose=True, factor=args.lr_gamma, threshold=1e-8)

    start_time = time.time()
    loss_ten=0
    for epoch in range(initial_epoch, args.epoch):
        epoch_loss = 0
        epoch_time = time.time()
        for n_count, batch_xy in enumerate(DLoader):
            optimizer.zero_grad()
            if cuda:
                batch_x, batch_y = batch_xy[0].cuda(), batch_xy[1].cuda()
            loss = criterion(model(batch_y)[:,:,loss_crop_lb:loss_crop_up, loss_crop_lb:loss_crop_up], batch_x[:,:,loss_crop_lb:loss_crop_up, loss_crop_lb:loss_crop_up])
            if args.lossf == 'SSIM':
                loss = 1 - loss
            epoch_loss += loss.item()
            loss_ten += loss.item()
            loss.backward()
            optimizer.step()
            if n_count % 10 == 0:
                print('%4d %4d / %4d loss = %2.4f' % (epoch+1, n_count, len(DDataset)//batch_size, loss_ten/10))
                loss_ten = 0
        if args.scheduler == 'plateau':
            scheduler.step(epoch_loss/n_count)
        else:
            scheduler.step(epoch)  # step to the learning rate in this epcoh
        elapsed_time = time.time() - epoch_time
        os.makedirs(save_dir, exist_ok=True)
        os.makedirs(res_dir, exist_ok=True)
        log('epoch = %4d , loss = %4.4f , time = %4.2f s' % (epoch+1, epoch_loss/n_count, elapsed_time))
        np.savetxt(res_dir+'/train_result_'+str(epoch)+'.txt', np.hstack((epoch+1, epoch_loss/n_count, elapsed_time)), fmt='%2.4f')
        # torch.save(model.state_dict(), os.path.join(save_dir, 'model_%03d.pth' % (epoch+1)))
        torch.save(model, os.path.join(save_dir, 'model_%03d.pth' % (epoch+1)))
        torch.save(model, os.path.join(save_dir, 'latest_model.pth'))
        if args.time_limit is not None and args.time_limit < time.time() - start_time:
            print('Time is up.')
            break

