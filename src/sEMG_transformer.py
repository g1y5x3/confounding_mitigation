import torch, argparse
import numpy as np
import scipy.io as sio
import torch.nn.functional as F
from torch import nn
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader
from torch.utils.tensorboard import SummaryWriter

def load_raw_signals(file):
  data = sio.loadmat(file)
  signals = data['DATA']
  labels = data['LABEL']
  vfi_1 = data['SUBJECT_VFI']
  sub_id = data['SUBJECT_ID']
  sub_skinfold = data['SUBJECT_SKINFOLD']
  return signals, labels, vfi_1, sub_id, sub_skinfold

class sEMGSignalDataset(Dataset):
  def __init__(self, signals, labels):
    self.signals = signals
    self.labels = labels

  def __len__(self):
    return len(self.labels)

  def __getitem__(self, idx):
    signal = torch.tensor(self.signals[idx,:,:], dtype=torch.float32)
    label = torch.tensor(self.labels[idx,:], dtype=torch.float32)
    return signal, label

class sEMGtransformer(nn.Module):
  def __init__(self, patch_size=64, d_model=512, nhead=8, dim_feedforward=2048, dropout=0.1, num_layers=1):
    super().__init__()
    self.patch_size = patch_size
    self.seq_len = 4000 // patch_size

    self.input_project = nn.Linear(4*self.patch_size, d_model)
    self.dropout = nn.Dropout(dropout)
    encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, dim_feedforward=dim_feedforward, dropout=dropout,
                                               activation=nn.GELU(), batch_first=True, norm_first=True)
    self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
    self.mlp_head = nn.Linear(d_model, 2)

    self.cls_token = nn.Parameter(torch.rand(1, 1, d_model))
    self.pos_embedding = nn.Parameter(torch.randn(1, self.seq_len+1, d_model))

  def forward(self, x):
    # convert from raw signals to signal patches
    x = x[:, :, :(x.shape[2] // self.patch_size)*self.patch_size]
    B, C, L = x.shape
    x = x.reshape(B, C, L//self.patch_size, self.patch_size)  # [B, C, seq_len, patch_size]
    x = x.permute(0, 2, 1, 3).flatten(2,3)                    # [B, seq_len, C*patch_size]
    x = self.input_project(x)                                 # [B, seq_len, d_model]

    # add class token and positional embedding
    cls_token = self.cls_token.repeat(B,1,1)
    x = torch.cat((cls_token, x), dim=1)
    x = x + self.pos_embedding[:,:(self.seq_len+1)]
    x = self.dropout(x)

    x = self.transformer_encoder(x)

    # compare to using only the cls_token, using mean of embedding has a much smoother loss curve
    x = x.mean(dim=1)
    x = self.mlp_head(x)
    return x


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="sEMG transformer training configurations")
  # training config
  parser.add_argument('-seed', type=int, default=0, help="random seed")
  parser.add_argument('-epochs', type=int, default=1000, help="number of epochs")
  parser.add_argument('-bsz', type=int, default=32, help="batch size")
  # optimizer config
  parser.add_argument('-lr', type=float, default=0.001, help="learning rate")
  parser.add_argument('-wd', type=float, default=0.01, help="weight decay")
  # model config
  parser.add_argument('-psz', type=int, default=64, help="signal patch size")
  parser.add_argument('-d_model', type=int, default=512, help="transformer embedding dim")
  parser.add_argument('-nhead', type=int, default=8, help="transformer number of attention heads")
  parser.add_argument('-dim_feedforward', type=int, default=2048, help="transformer feed-forward dim")
  parser.add_argument('-num_layers', type=int, default=1, help="number of transformer encoder layers")
  parser.add_argument('-dropout', type=float, default=0.1, help="dropout rate")
  args = parser.parse_args()

  # to stay sane
  np.random.seed(args.seed)
  torch.manual_seed(args.seed)

  # signal pre-processing
  signals, labels, vfi_1, sub_id, sub_skinfold = load_raw_signals("data/subjects_40_v6.mat")

  X, Y = [], []
  for i in range(40):
    # stack all inputs into [N,C,L] format
    x = np.stack(signals[i], axis=1)
    # one-hot encode the binary labels
    N = labels[i][0].shape[0]
    mapped_indices = (labels[i][0] == 1).astype(int)
    y_onehot = np.zeros((N, 2))
    y_onehot[np.arange(N), mapped_indices.flatten()] = 1

    X.append(x)
    Y.append(y_onehot)

  X, Y = np.concatenate(X, axis=0), np.concatenate(Y, axis=0)
  print(f"X {X.shape}")
  print(f"Y {Y.shape}")

  # normalize X channel-wise
  X_means = np.mean(X, axis=(0,2))
  X_stds = np.std(X, axis=(0,2))
  X_norm = (X - X_means[np.newaxis,:,np.newaxis]) / X_stds[np.newaxis,:,np.newaxis]
  print(f"X means {X_means}")
  print(f"X stds {X_stds}")

  # split training and validation
  num_samples = X_norm.shape[0]
  indices = np.arange(num_samples)
  np.random.shuffle(indices)
  split_idx = int(num_samples*0.9)
  train_idx, valid_idx = indices[:split_idx], indices[split_idx:]

  X_train, X_valid = X_norm[train_idx], X_norm[valid_idx]
  Y_train, Y_valid = Y[train_idx], Y[valid_idx]
  print(f"X_train {X_train.shape}")
  print(f"Y_train {Y_train.shape}")
  print(f"X_valid {X_valid.shape}")
  print(f"Y_valid {Y_valid.shape}")

  dataset_train = sEMGSignalDataset(X_train, Y_train)
  dataset_valid = sEMGSignalDataset(X_valid, Y_valid)

  dataloader_train = DataLoader(dataset_train, batch_size=args.bsz, shuffle=True)
  dataloader_valid = DataLoader(dataset_valid, batch_size=args.bsz, shuffle=False)

  model = sEMGtransformer(patch_size=args.psz, d_model=args.d_model, nhead=args.nhead, dim_feedforward=args.dim_feedforward, dropout=args.dropout,
                          num_layers=args.num_layers)
  model.to("cuda")
  # TODO: more tests
  # model_compiled = torch.compile(model)

  criterion = nn.CrossEntropyLoss()
  optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
  scaler = torch.cuda.amp.GradScaler()

  writer = SummaryWriter()
  accuracy_best = 0
  for epoch in tqdm(range(args.epochs), desc="Training"):
    loss_train = 0
    correct_train = 0
    model.train()
    for batch, (inputs, targets) in enumerate(dataloader_train):
      inputs, targets = inputs.to("cuda"), targets.to("cuda")
      optimizer.zero_grad()
      with torch.autocast(device_type="cuda", dtype=torch.float16):
        outputs = model(inputs)
        # outputs = model_compiled(inputs)
        loss = criterion(outputs, targets)

      scaler.scale(loss).backward()
      scaler.step(optimizer)
      scaler.update()

      _, predicted = torch.max(F.softmax(outputs, dim=1), 1)
      _, labels    = torch.max(targets, 1)
      correct_train += (predicted == labels).sum().item()
      loss_train += loss.item()

    writer.add_scalar("loss/train", loss_train/len(dataset_train), epoch)
    writer.add_scalar("accuracy/train", correct_train/len(dataset_train), epoch)

    loss_valid = 0
    correct_valid = 0
    model.eval()
    for inputs, targets in dataloader_valid:
      inputs, targets = inputs.to("cuda"), targets.to("cuda")
      outputs = model(inputs)
      # outputs = model_compiled(inputs)
      loss = criterion(outputs, targets)

      _, predicted = torch.max(F.softmax(outputs, dim=1), 1)
      _, labels    = torch.max(targets, 1)
      correct_valid += (predicted == labels).sum().item()
      loss_valid += loss.item()

    writer.add_scalar("loss/valid", loss_valid/len(dataset_valid), epoch)
    writer.add_scalar("accuracy/valid", correct_valid/len(dataset_valid), epoch)

    if correct_valid/len(dataset_valid) > accuracy_best: accuracy_best = correct_valid/len(dataset_valid)

  writer.add_hparams(vars(args), {"accuracy_best": accuracy_best})
  writer.close()

  # leave-one-out testing
