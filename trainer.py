import torch
import torch.nn as nn
import time
import numpy as np
import torch.optim as optim
import torch.nn.functional as F
from tqdm import tqdm
import os
import json

from utils import Logger
from utils import cal_correct_num,get_sentence
tosave = ['acc']
class Trainer:
    def __init__(self,cfg,datasets,net,loss,epoch):
        self.cfg = cfg
        if 'train' in datasets:
            self.trainset = datasets['train']
            self.valset = datasets['val']
        if 'trainval' in datasets:
            self.trainval = datasets['trainval']
        else:
            self.trainval = False
        if 'test' in datasets:
            self.testset = datasets['test']
        self.net = net
        name = cfg.exp_name
        self.name = name
        self.checkpoints = os.path.join(cfg.checkpoint,name)
        self.device = cfg.device
        self.net = self.net
        self.optimizer = optim.Adam(self.net.parameters(),lr=cfg.lr,weight_decay=cfg.weight_decay)
        self.lr_sheudler = optim.lr_scheduler.ReduceLROnPlateau(self.optimizer,mode='min', factor=cfg.lr_factor, threshold=0.0001,patience=cfg.patience,min_lr=cfg.min_lr)
        if not(os.path.exists(self.checkpoints)):
            os.mkdir(self.checkpoints)
        self.predictions = os.path.join(self.checkpoints,'pred')
        if not(os.path.exists(self.predictions)):
            os.mkdir(self.predictions)
        start,total = epoch       
        self.total = total
        self.loss = loss
        log_dir = os.path.join(self.checkpoints,'logs')
        if not(os.path.exists(log_dir)):
            os.mkdir(log_dir)
        self.logger = Logger(log_dir)
        torch.cuda.empty_cache()
        self.save_every_k_epoch = cfg.save_every_k_epoch #-1 for not save and validate
        self.val_every_k_epoch = cfg.val_every_k_epoch
        self.upadte_grad_every_k_batch = 1

        self.best_acc = 0
        self.best_acc_epoch = 0

        self.movingAvg = 0
        self.bestMovingAvg = 0
        self.bestMovingAvgEpoch = 1e9
        self.early_stop_epochs = 50
        self.alpha = 0.95 #for update moving Avg
        self.nms_threshold = cfg.nms_threshold
        self.conf_threshold = cfg.dc_threshold
        self.save_pred = False
        self.adjust_lr = cfg.adjust_lr
        self.fine_tune = cfg.fine_tune
        #load from epoch if required
        if start:
            if start=='-1':
                self.load_last_epoch()
            else:
                self.load_epoch(start.strip())
        else:
            self.start = 0
        self.net = self.net.to(self.device)
    def load_last_epoch(self):
        files = os.listdir(self.checkpoints)
        idx = 0
        for name in files:
            if name[-3:]=='.pt':
                epoch = name[6:-3]
                if epoch=='best' or epoch=='bestm':
                  continue
                idx = max(idx,int(epoch))
        if idx==0:
            exit()
        else:
            self.load_epoch(str(idx))
    def save_epoch(self,idx,epoch):
        saveDict = {'net':self.net.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'lr_scheduler':self.lr_sheudler.state_dict(),
                    'epoch':epoch,
                    'acc':self.best_acc,
                    'best_epoch':self.best_acc_epoch,
                    'movingAvg':self.movingAvg,
                    'bestmovingAvg':self.bestMovingAvg,
                    'bestmovingAvgEpoch':self.bestMovingAvgEpoch}
        path = os.path.join(self.checkpoints,'epoch_'+idx+'.pt')
        torch.save(saveDict,path)                  
    def load_epoch(self,idx):
        model_path = os.path.join(self.checkpoints,'epoch_'+idx+'.pt')
        if os.path.exists(model_path):
            print('load:'+model_path)
            info = torch.load(model_path)
            self.net.load_state_dict(info['net'])
            if not(self.adjust_lr):
                self.optimizer.load_state_dict(info['optimizer'])#might have bugs about device
                for state in self.optimizer.state.values():
                    for k, v in state.items():
                        if isinstance(v, torch.Tensor):
                            state[k] = v.to(self.device)
                self.lr_sheudler.load_state_dict(info['lr_scheduler'])
            self.start = info['epoch']+1
            self.best_acc = info['acc']
            self.best_acc_epoch = info['best_epoch']
            self.movingAvg = info['movingAvg']
            self.bestMovingAvg = info['bestmovingAvg']
            self.bestMovingAvgEpoch = info['bestmovingAvgEpoch']
        else:
            print('no such model at:',model_path)
            exit()
    def _updateMetrics(self,mAP,epoch):
        if self.movingAvg ==0:
            self.movingAvg = mAP
        else:
            self.movingAvg = self.movingAvg * self.alpha + mAP*(1-self.alpha)
        if self.bestMovingAvg<self.movingAvg:
            self.bestMovingAvg = self.movingAvg
            self.bestMovingAvgEpoch = epoch
            self.save_epoch('bestm',epoch)
    def logMemoryUsage(self, additionalString=""):
        if torch.cuda.is_available():
            print(additionalString + "Memory {:.0f}Mb max, {:.0f}Mb current".format(
                torch.cuda.max_memory_allocated() / 1024 / 1024, torch.cuda.memory_allocated() / 1024 / 1024))

    def train_one_epoch(self):
        running_loss ={'all':0.0}
        self.net.train()
        n = len(self.trainset)
        self.loss.not_match = 0
        for i,data in tqdm(enumerate(self.trainset)):
            inputs,labels = data
            outs = self.net(inputs.to(self.device).float())
            labels = labels.to(self.device).float()
            display,loss = self.loss(outs,labels)
            del inputs,outs,labels
            for k in running_loss:
                if k in display.keys():
                    running_loss[k] += display[k]/n
            loss.backward()
            #solve gradient explosion problem caused by large learning rate or small batch size
            #nn.utils.clip_grad_value_(self.net.parameters(), clip_value=2.0) 
            nn.utils.clip_grad_norm_(self.net.parameters(),max_norm=2.0)
            if i == n-1 or (i+1) % self.upadte_grad_every_k_batch == 0:
                self.optimizer.step()
                self.optimizer.zero_grad()
            del loss
        self.logMemoryUsage()
        print(f'#Gt not matched:{self.loss.not_match}')
        self.loss.reset_notmatch()
        return running_loss
    def train(self):
        print("strat train:",self.name)
        print("start from epoch:",self.start)
        print("=============================")
        self.optimizer.zero_grad()
        print(self.optimizer.param_groups[0]['lr'])
        epoch = self.start
        stop_epochs = 0
        #torch.autograd.set_detect_anomaly(True)
        while epoch < self.total and stop_epochs<self.early_stop_epochs:
            running_loss = self.train_one_epoch()            
            lr = self.optimizer.param_groups[0]['lr']
            self.logger.write_loss(epoch,running_loss,lr)
            #step lr
            self.lr_sheudler.step(running_loss['all'])
            lr_ = self.optimizer.param_groups[0]['lr']
            if lr_ <= self.cfg.min_lr+1e-16:
                stop_epochs +=1
            if lr_ != lr:
                self.save_epoch(str(epoch),epoch)
            if (epoch+1)%self.save_every_k_epoch==0:
                self.save_epoch(str(epoch),epoch)
            if (epoch+1)%self.val_every_k_epoch==0:                
                metrics = self.validate(epoch,'val',self.save_pred)
                self.logger.write_metrics(epoch,metrics,tosave)
                acc = metrics['acc']
                if acc >= self.best_acc:
                    self.best_acc = acc
                    self.best_acc_epoch = epoch
                    self.save_epoch('best',epoch)
                print(f"best so far with {self.best_acc} at epoch:{self.best_acc_epoch}")
                if self.trainval:
                    metrics = self.validate(epoch,'train',self.save_pred)
                    self.logger.write_metrics(epoch,metrics,tosave,mode='Trainval')
                    acc = metrics['acc']
            epoch +=1
                
        print("Best Accuracy: {:.4f} at epoch {}".format(self.best_acc, self.best_acc_epoch))
        self.save_epoch(str(epoch-1),epoch-1)
    def validate(self,epoch,mode,save=False):
        self.net.eval()
        res = {}
        print('start Validation Epoch:',epoch)
        if mode=='val':
            valset = self.valset
        else:
            valset = self.trainval
        with torch.no_grad():
            n_gt = 0
            n_pd = 0
            n_cor = 0
            for data in tqdm(valset):
                inputs,labels = data
                outs = self.net(inputs.to(self.device).float())
                pds = self.loss(outs,infer=True)
                nB = pds.shape[0]
                n_gt += len(labels)             
                for b in range(nB):
                    pd = outs[b]
                    gt = list(labels[labels[:,0]==b,1].numpy())
                    n_pd += len(pd)
                    n_cor += cal_correct_num(pd,gt)
                    
        metrics={'acc'}
        if save:
            json.dump(res,open(os.path.join(self.predictions,'pred_epoch_'+str(epoch)+'.json'),'w'))
        
        return metrics
    def test(self):
        self.net.eval()
        res = {}
        with torch.no_grad():
            for _,data in tqdm(enumerate(self.testset)):
                inputs,info = data
                outs = self.net(inputs.to(self.device).float())
                size = inputs.shape[-2:]
                pds = self.loss(outs,size=size,infer=True)
                nB = pds.shape[0]
                for b in range(nB):
                    pred = pds[b].view(-1,self.cfg.cls_num+5)
                    name = info['img_id'][b]
                    tsize = info['size'][b]
                    pad = info['pad'][b]
                    pred[:,:4]*=max(tsize)
                    pred[:,0] -= pad[1]
                    pred[:,1] -= pad[0]
                    cls_confs,cls_labels = torch.max(pred[:,5:],dim=1,keepdim=True)
                    pred_nms = torch.cat((pred[:,:5],cls_confs,cls_labels.float()),dim=1)                    
                    #pred_nms = nms(pred,self.conf_threshold, self.nms_threshold)
                    pds_ = list(pred_nms.cpu().numpy().astype(float))
                    pds_ = [list(pd) for pd in pds_]
                    res[name] = pds_
        
        json.dump(res,open(os.path.join(self.predictions,'pred_test.json'),'w'))

        


                


        



