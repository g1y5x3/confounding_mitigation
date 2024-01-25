import os
import wandb
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import Sampler, Dataset
from fastai.learner import Learner
from fastai.data.core import DataLoader, DataLoaders
from fastai.callback.wandb import WandbCallback
from tsai.all import get_splits, MLP
from sklearn.metrics import accuracy_score
from util.sEMGhelpers import load_datafile, partition
from cpt import conditional_log_likelihood, cpt_p_pearson, cpt_p_pearson_torch

# environment variable for the experiment
WANDB = os.getenv("WANDB", False)
NAME  = os.getenv("NAME",  "Confounding-Mitigation-In-Deep-Learning")
GROUP = os.getenv("GROUP", "MLP-sEMG-CPT")

class sEMGSampler(Sampler):
  def __init__(self, sids):
    self.sids = sids

  def __len__(self):
    return len(self.sids)

  def __iter__(self):
    return iter((0)*len(self.sids))

class sEMGDataset(Dataset):
  def __init__(self, X, Y, C, index, sid, train=False):
    self.X = X
    self.Y = Y
    self.C = C
    self.i = index
    self.sid = sid
    self.train = train

  def __len__(self):
    return len(self.Y)

  def __getitem__(self, idx):
    print(idx)
    x = torch.tensor(self.X[idx,:], dtype=torch.float32)
    c = torch.tensor(self.C[idx],   dtype=torch.float32)
    i = torch.tensor(self.i[idx],   dtype=torch.int)
    y = torch.tensor(self.Y[idx],   dtype=torch.long)
    return (x, c, i), y

# model is the same but passing an additional input variable
class MLPC(MLP):
  def __init__(self, c_in, c_out, seq_len, layers,
               ps=[0.1, 0.2, 0.2], act=nn.ReLU(inplace=True), use_bn=False, bn_final=False, lin_first=False, fc_dropout=0., y_range=None):
    super().__init__(c_in, c_out, seq_len, layers, ps, act, use_bn, bn_final, lin_first, fc_dropout, y_range)

  def forward(self, x_c_idx):
    x, c, idx = x_c_idx
    return (super().forward(x), c, idx)

class CrossEntropyLoss(nn.Module):
  def __init__(self):
    super().__init__()

  def __call__(self, yhat_c_idx, y):
    yhat, _, _ = yhat_c_idx
    return F.cross_entropy(yhat, y)

class CrossEntropyCPTLoss(nn.Module):
  def __init__(self, cond_like_mat, mcmc_steps=50, random_state=123, num_perm=1000):
    super().__init__()
    self.mcmc_steps        = mcmc_steps   # this is the default value used inside original CPT function
    self.random_state      = random_state
    self.num_perm          = num_perm
    self.cond_log_like_mat = cond_like_mat

  def __call__(self, yhat_c_idx, y):
    yhat, c, idx = yhat_c_idx
    p = cpt_p_pearson_torch(c.numpy(), yhat.argmax(dim=1), self.cond_log_like_mat[idx,:][:,idx], self.mcmc_steps, self.random_state, self.num_perm)
    l = F.cross_entropy(yhat, y)
    print(f"cross entropy {l}")
    print(f"p-value {p}")

    return l

def accuracy(preds_confound_index, targets):
  preds, _, _ = preds_confound_index
  return (preds.argmax(dim=-1) == targets).float().mean()



if __name__ == "__main__":
  FEAT_N, LABEL, SUBJECT_SKINFOLD, VFI_1, SUBJECT_ID = load_datafile("data/subjects_40_v6")

  train_acc = np.zeros(40)
  valid_acc = np.zeros(40)
  test_acc  = np.zeros(40)
  p_value   = np.zeros(40)
  for sub_test in range(0, 1):
    sub_txt = "R%03d"%(int(SUBJECT_ID[sub_test][0][0]))
    sub_group = "Fatigued" if int(VFI_1[sub_test][0][0][0]) > 10 else "Healthy"
    print('\n===No.%d: %s===\n'%(sub_test+1, sub_txt))
    print('VFI-1:', (VFI_1[sub_test][0][0]))

    cbs = None
    if WANDB:
      run = wandb.init(project=NAME, group=GROUP, name=sub_txt, tags=[sub_group], reinit=True)
      cbs = WandbCallback(log_preds=False)

    print("Loading training and testing set")
    X_Train, Y_Train, C_Train, ID_Train, X_Test, Y_Test, C_Test, ID_Test = partition(FEAT_N, LABEL, SUBJECT_SKINFOLD, sub_test, SUBJECT_ID)
    Y_Train = np.where(Y_Train == -1, 0, 1)
    Y_Test  = np.where(Y_Test  == -1, 0, 1)

    # Setting "stratify" to True ensures that the relative class frequencies are approximately preserved in each train and validation fold.
    cond_like_mat = conditional_log_likelihood(X=C_Train, C=Y_Train, xdtype='categorical')
    splits = get_splits(Y_Train, valid_size=.1, stratify=True, random_state=123, shuffle=True, show_plot=False)
    dsets_train = sEMGDataset(X_Train[splits[0],:], Y_Train[splits[0]], C_Train[splits[0]], splits[0], ID_Train[splits[0]])
    dsets_valid = sEMGDataset(X_Train[splits[1],:], Y_Train[splits[1]], C_Train[splits[1]], splits[1], ID_Train[splits[1]])
    dsets_test  = sEMGDataset(X_Test, Y_Test, C_Test, list(np.arange(len(X_Test))), ID_Test)

    train_sampler = sEMGSampler(ID_Train[splits[0]])

    bs = 256
    print(f"batch size: {bs}")
    train_dl = DataLoader(dsets_train, batch_size=bs, sampler=train_sampler, num_workers=1, pin_memory=True)
    valid_dl = DataLoader(dsets_valid, batch_size=bs, shuffle=False, num_workers=4, pin_memory=True)
    test_dl  = DataLoader(dsets_test, batch_size=bs, shuffle=False, num_workers=4, pin_memory=True)
    dls = DataLoaders(train_dl, valid_dl)

    # dls = DataLoaders.from_dsets(dsets_train, dsets_valid, shuffle=False, bs=bs, sampler = train_sampler, num_workers=4, pin_memory=True)

    # This model is pre-defined in https://timeseriesai.github.io/tsai/models.mlp.html
    model = MLPC(c_in=1, c_out=2, seq_len=48, layers=[50, 50, 50], use_bn=True)
    learn = Learner(dls, model, loss_func=CrossEntropyCPTLoss(cond_like_mat), metrics=[accuracy], cbs=cbs)

    # main training loop is abstracted
    learn.lr_find()
    learn.fit_one_cycle(5, lr_max=1e-3)

    # Training accuracy
    train_output, train_targets = learn.get_preds(dl=dls.train, with_loss=False)
    train_preds, train_c, _ = train_output
    train_acc[sub_test] = accuracy_score(train_targets, train_preds.argmax(dim=1))
    print(f"Training acc   : {train_acc[sub_test]}")

    # P-value (only makes sense to report for training)
    p, _ = cpt_p_pearson(train_c.numpy(), train_preds.argmax(dim=1).numpy(), train_targets.numpy(), cond_like_mat[splits[0],:][:,splits[0]],
                                mcmc_steps=100, random_state=None, num_perm=2000, dtype='categorical')
    p_value[sub_test] = p
    print(f"P Value        : {p}")

    # Validation accuracy
    valid_output, valid_targets = learn.get_preds(dl=dls.valid, with_loss=False)
    valid_preds, valid_c, _ = valid_output
    valid_acc[sub_test] = accuracy_score(valid_targets, valid_preds.argmax(dim=1))
    print(f"Validation acc : {valid_acc[sub_test]}")

    # Testing accuracy
    dls_test = dls.new(dsets_test)
    learn.loss_func = CrossEntropyLoss()
    learn.metrics = accuracy
    test_output, test_targets = learn.get_preds(dl=dls_test, with_loss=False)
    test_preds, test_c, _ = test_output
    test_acc[sub_test] = accuracy_score(test_targets, test_preds.argmax(dim=1))
    print(f"Testing acc : {test_acc[sub_test]}")

    if WANDB: wandb.log({"subject_info/vfi_1" : int(VFI_1[sub_test][0][0]),
                         "metrics/train_acc_opt"  : train_acc[sub_test],
                         "metrics/test_acc_opt"   : test_acc[sub_test],
                         "metrics/p_value_opt"    : p_value[sub_test]})