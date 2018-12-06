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
from torch.optim.lr_scheduler import MultiStepLR, LambdaLR
from dataset_torch_3 import DenoisingDataset
from torch.nn.modules.loss import _Loss
from lib import pytorch_msssim
from random import randint

# Params
parser = argparse.ArgumentParser(description='PyTorch DnCNN')
parser.add_argument('--model', default='DnCNN', type=str, help='Model type (default: DnCNN)')
parser.add_argument('--batch_size', default=32, type=int, help='batch size')
parser.add_argument('--train_data', default='datasets/train/ds_128_96', type=str, help='path to the train data (default: '+'datasets/train/dataset_128_96'+')')
parser.add_argument('--epoch', default=96, type=int, help='number of train epoches')
parser.add_argument('--lr', default=1e-3, type=float, help='initial learning rate for Adam')
parser.add_argument('--expname', type=str, help='Experiment name used to save and/or load results and models (default autogenerated from time+CLI)')
parser.add_argument('--resume', action='store_true', help='Look for an experiment with the same parameters and continue (to force continuing an experiment with different parameters use --expname instead)')
parser.add_argument('--result_dir', default='results/train', type=str, help='Directory where results are stored (default: results/train)')
parser.add_argument('--models_dir', default='models', type=str, help='Directory where models are saved/loaded (default: models)')
parser.add_argument('--depth', default=22, type=int, help='Number of layers (default: 22)')
parser.add_argument('--cuda_device', default=0, type=int, help='Device number (default: 0, typically 0-3)')
parser.add_argument('--n_channels', default=128, type=int, help='Number of channels (default: 128)')
parser.add_argument('--find_noise', action='store_true', help='Model noise if set otherwise generate clean image')
parser.add_argument('--kernel_size', default=5, type=int, help='Kernel size')
parser.add_argument('--docompression', type=str, help='Add compression to noisy images (random or [1-100], off if omitted)')
parser.add_argument('--random_lr', action='store_true', help='Random learning rate (changes after each epoch)')
parser.add_argument('--lossf', default='MSSSIM', help='Loss class defined in lib/__init__.py')
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
    print('===> Building model')
    if args.model == 'DnCNN':
        model = nnModules.DnCNN(depth=args.depth, n_channels=args.n_channels, find_noise=args.find_noise, kernel_size=args.kernel_size)
    elif args.model == 'RedCNN':
        model = nnModules.RedCNN(depth=args.depth, n_channels=args.n_channels, kernel_size=args.kernel_size)
    elif args.model == 'RedishCNN':
        model = nnModules.RedishCNN(depth=args.depth, n_channels=args.n_channels, kernel_size=args.kernel_size)
    else:
        exit(args.model+' not implemented.')
    initial_epoch = findLastCheckpoint(save_dir=save_dir)  # load the last model in matconvnet style
    if initial_epoch > 0:
        print('resuming by loading epoch %03d' % initial_epoch)
        # model.load_state_dict(torch.load(os.path.join(save_dir, 'model_%03d.pth' % initial_epoch)))
        model = torch.load(os.path.join(save_dir, 'model_%03d.pth' % initial_epoch))
    model.train()
    if args.lossf == 'MSSSIM':
        criterion = pytorch_msssim.MSSSIM(channel=3)
    elif args.lossf == 'MSSSIMandMSE':
        criterion = pytorch_msssim.MSSSIMandMSE()
    else:
        exit('Error: requested loss function '+args.lossf+' has not been implemented.')
    if cuda:
        model = model.cuda()
        # device_ids = [0]
        # model = nn.DataParallel(model, device_ids=device_ids).cuda()
        # criterion = criterion.cuda()
    if not args.docompression:
        docompression=False
    else:
        docompression=args.docompression
    DDataset = DenoisingDataset(args.train_data, docompression=docompression)
    DLoader = DataLoader(dataset=DDataset, num_workers=8, drop_last=True, batch_size=batch_size, shuffle=True)
    loss_crop_lb = int((DDataset.cs-DDataset.ucs)/2)
    loss_crop_up = loss_crop_lb+DDataset.ucs
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    if args.random_lr:
        lrlambda = lambda epoch, lr=args.lr: randint(1,.1/lr)/randint(1,1/lr)
        scheduler = LambdaLR(optimizer, lrlambda)
    else:
        scheduler = MultiStepLR(optimizer, milestones=[args.epoch*.02, args.epoch*.06, args.epoch*.14, args.epoch*.30, args.epoch*.62, args.epoch*.78, args.epoch*.86], gamma=0.5)  # learning rates
    for epoch in range(initial_epoch, args.epoch):
        scheduler.step(epoch)  # step to the learning rate in this epcoh
        epoch_loss = 0
        start_time = time.time()

        for n_count, batch_yx in enumerate(DLoader):
            optimizer.zero_grad()
            if cuda:
                batch_x, batch_y = batch_yx[1].cuda(), batch_yx[0].cuda()

            loss = criterion(model(batch_y)[:,:,loss_crop_lb:loss_crop_up, loss_crop_lb:loss_crop_up], batch_x[:,:,loss_crop_lb:loss_crop_up, loss_crop_lb:loss_crop_up])
            epoch_loss += loss.item()
            loss.backward()
            optimizer.step()
            if n_count % 10 == 0:
                print('%4d %4d / %4d loss = %2.4f' % (epoch+1, n_count, len(DDataset)//batch_size, loss.item()/batch_size))
        elapsed_time = time.time() - start_time
        os.makedirs(save_dir, exist_ok=True)
        os.makedirs(res_dir, exist_ok=True)
        log('epoch = %4d , loss = %4.4f , time = %4.2f s' % (epoch+1, epoch_loss/n_count, elapsed_time))
        np.savetxt(res_dir+'/train_result_'+str(epoch)+'.txt', np.hstack((epoch+1, epoch_loss/n_count, elapsed_time)), fmt='%2.4f')
        # torch.save(model.state_dict(), os.path.join(save_dir, 'model_%03d.pth' % (epoch+1)))
        torch.save(model, os.path.join(save_dir, 'model_%03d.pth' % (epoch+1)))
        torch.save(model, os.path.join(save_dir, 'latest_model.pth'))





