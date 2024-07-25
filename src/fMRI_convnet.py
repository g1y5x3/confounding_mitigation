import os, wandb, argparse, requests, torch
import torch.optim as optim
import torch.nn.functional as F
from torch import nn, optim
from torch.utils.data import DataLoader
from torchinfo import summary
from tqdm import trange
from util.fMRIImageLoader import IXIDataset, CenterRandomShift, RandomMirror

DATA_DIR   = os.getenv("DATA_DIR",   "data/IXI_4x4x4")
DATA_SPLIT = os.getenv("DATA_SPLIT", "all")

def generate_wandb_name(config):
  train_sites = '_'.join(sorted(config['site_train']))
  test_sites = '_'.join(sorted(config['site_test']))
  return f"train_{train_sites}_test_{test_sites}"

class SFCN(nn.Module):
  def __init__(self, channel_number=[32, 64, 128, 256, 256, 64], output_dim=40, dropout=True):
    super(SFCN, self).__init__()
    n_layer = len(channel_number)

    self.feature_extractor = nn.Sequential()
    for i in range(n_layer):
      in_channel = 1 if i == 0 else channel_number[i-1] 
      out_channel = channel_number[i]
      if i < n_layer-1:
        self.feature_extractor.add_module(f"conv_{i}",
                                          self.conv_layer(in_channel,
                                                          out_channel,
                                                          maxpool=True,
                                                          kernel_size=3,
                                                          padding=1))
      else:
        self.feature_extractor.add_module(f"conv_{i}",
                                          self.conv_layer(in_channel,
                                                          out_channel,
                                                          maxpool=False,
                                                          kernel_size=1,
                                                          padding=0))

    self.classifier = nn.Sequential()
    # NOTE initial model uses a average pool here
    if dropout: self.classifier.add_module('dropout', nn.Dropout(0.5))
    i = n_layer
    # TODO calculate or ask user to provide the dim size of handcoding it
    # otherwise this would have to change depends on the input image size
    in_channel = channel_number[-1]*2*2*2
    out_channel = output_dim
    self.classifier.add_module(f"fc_{i}", nn.Linear(in_channel, out_channel))

  @staticmethod
  def conv_layer(in_channel, out_channel, maxpool=True, kernel_size=3, padding=0, maxpool_stride=2):
    if maxpool is True:
      layer = nn.Sequential(
        nn.Conv3d(in_channel, out_channel, padding=padding, kernel_size=kernel_size),
        nn.BatchNorm3d(out_channel),
        nn.MaxPool3d(2, stride=maxpool_stride),
        nn.ReLU(),
      )
    else:
      layer = nn.Sequential(
        nn.Conv3d(in_channel, out_channel, padding=padding, kernel_size=kernel_size),
        nn.BatchNorm3d(out_channel),
        nn.ReLU()
      )
    return layer

  def forward(self, x):
    x = self.feature_extractor(x)
    x = x.view(x.size(0), -1)
    x = self.classifier(x)
    x = F.softmax(x, dim=1)
    return x


def train(config, run=None):
  device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

  print(config) 

  # TODO: Test with other range that does not produce a x64 output
  bin_range   = [21,85]

  print("\nDataloader:")
  # based on the paper the training inputs are 
  # 1) randomly shifted by 0, 1, or 2 voxels along every axis; 
  # 2) has a probability of 50% to be mirrored about the sagittal plane
  # site_test = ["Guys", "HH", "IOP"]
  site_train = ["Guys", "HH"]
  data_train = IXIDataset(data_dir=DATA_DIR, label_file=f"IXI_{DATA_SPLIT}_train.csv",
                          sites=site_train,
                          bin_range=bin_range, 
                          transform=[CenterRandomShift(randshift=True), RandomMirror()])

  # site_test = ["Guys", "HH", "IOP"]
  site_test = ["IOP"]
  data_test = IXIDataset(data_dir=DATA_DIR, label_file=f"IXI_{DATA_SPLIT}_test.csv",  
                         sites=site_test, 
                         bin_range=bin_range,
                         transform=[CenterRandomShift(randshift=False)])

  bin_center = data_train.bin_center.reshape([-1,1])

  dataloader_train = DataLoader(data_train, batch_size=config["bs"], num_workers=config["num_workers"], pin_memory=True, shuffle=True)
  dataloader_test  = DataLoader(data_test,  batch_size=config["bs"], num_workers=config["num_workers"], pin_memory=True, shuffle=False)
  
  x, y = next(iter(dataloader_train))
  print("\nTraining data summary:")
  print(f"Total data: {len(data_train)}")
  print(f"Input {x.shape}")
  print(f"Label {y.shape}")
  
  x, y = next(iter(dataloader_test))
  print("\nTesting data summary:")
  print(f"Total data: {len(data_test)}")
  print(f"Input {x.shape}")
  print(f"Label {y.shape}")
  
  model = SFCN(output_dim=y.shape[1])
  print(f"\nModel Dtype: {next(model.parameters()).dtype}")
  summary(model, x.shape)

  # load pretrained weights shared by the original author
  url = "https://github.com/ha-ha-ha-han/UKBiobank_deep_pretrain/raw/master/brain_age/run_20190719_00_epoch_best_mae.p"
  filename = "run_20190719_00_epoch_best_mae.pth" 
  if not os.path.exists(filename):
    response = requests.get(url)
    with open("run_20190719_00_epoch_best_mae.pth", "wb") as file:
      file.write(response.content)

  w_pretrained = torch.load("run_20190719_00_epoch_best_mae.pth")
  w_feature_extractor = {k: v for k, v in w_pretrained.items() if "module.classifier" not in k}
  model.load_state_dict(w_feature_extractor, strict=False)
  
  criterion = nn.KLDivLoss(reduction="batchmean", log_target=True)
  optimizer = optim.SGD(model.parameters(), lr=config["lr"], weight_decay=config["wd"])
  scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=config["step_size"], gamma=config["gamma"])
  scaler = torch.cuda.amp.GradScaler(enabled=True)
  
  # main training loop
  print(criterion)
  model.to(device)
  bin_center = bin_center.to(device)

  MAE_age_test_best = float('inf')
  
  t = trange(config["epochs"], desc="\nTraining", leave=True)
  for epoch in t:
    loss_train = 0.0
    MAE_age_train = 0.0
    for images, labels in dataloader_train:
      images, labels = images.to(device), labels.to(device)
      with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=True):
        output = model(images)
        loss = criterion(output.log(), labels.log())
      scaler.scale(loss).backward()
      scaler.step(optimizer)
      scaler.update()
      optimizer.zero_grad()
  
      with torch.no_grad():
        age_target = labels @ bin_center
        age_pred   = output @ bin_center
        MAE_age = F.l1_loss(age_pred, age_target, reduction="mean")
  
        loss_train += loss.item()
        MAE_age_train += MAE_age.item()
  
    loss_train = loss_train / len(dataloader_train)
    MAE_age_train = MAE_age_train / len(dataloader_train)
  
    with torch.no_grad():
      loss_test = 0.0
      MAE_age_test = 0.0
      for images, labels in dataloader_test:
        x, y = images.to(device), labels.to(device)
        output = model(x)
        loss = criterion(output.log(), y.log())
  
        age_target = y @ bin_center
        age_pred   = output @ bin_center
        MAE_age = F.l1_loss(age_pred, age_target, reduction="mean")
  
        loss_test += loss.item()
        MAE_age_test += MAE_age.item()
  
    loss_test = loss_test / len(dataloader_test)
    MAE_age_test = MAE_age_test / len(dataloader_test)

    if MAE_age_test < MAE_age_test_best:
      MAE_age_test_best = MAE_age_test
  
    scheduler.step()
  
    t.set_description(f"Training: train/loss {loss_train:.2f}, train/MAE_age {MAE_age_train:.2f} test/loss {loss_test:.2f}, test/MAE_age {MAE_age_test:.2f}")

    wandb.log({"train/loss": loss_train,
               "train/MAE_age": MAE_age_train,
               "test/loss":  loss_test,
               "test/MAE_age":  MAE_age_test,
               })
  
  # Save and upload the trained model 
  torch.save(model.state_dict(), "model.pth")

  artifact = wandb.Artifact("model", type="model")
  artifact.add_file("model.pth")
  run.log_artifact(artifact)

  wandb.run.summary["test/MAE_age_best"] = MAE_age_test_best
  print(f"\nTraining completed. Best MAE_age achieved: {MAE_age_test_best:.4f}")

  run.finish()

  return loss_test, MAE_age_test

if __name__ == "__main__":

  parser = argparse.ArgumentParser(description="Example:")
  parser.add_argument("--bs", type=int,   default=8,    help="batch size")
  parser.add_argument("--num_workers", type=int,   default=2,    help="number of workers")
  parser.add_argument("--epochs", type=int,   default=10,   help="total number of epochs")
  parser.add_argument("--lr", type=float, default=1e-2, help="learning rate")
  parser.add_argument("--wd", type=float, default=1e-3, help="weight decay")
  parser.add_argument("--step_size", type=int,   default=30,   help="step size")
  parser.add_argument("--gamma", type=float, default=0.3,  help="gamma")
  # specify training and testing site
  parser.add_argument("--site_train", nargs='+', default=["Guys", "HH", "IOP"], 
                      help="List of sites for training data (e.g., --site_train Guys HH)")
  parser.add_argument("--site_test", nargs='+', default=["Guys", "HH", "IOP"], 
                      help="List of sites for testing data (e.g., --site_test IOP)")
  args = parser.parse_args()
  config = vars(args)

  wandb_name = generate_wandb_name(config)

  run = wandb.init(project="fMRI-ConvNets", name=wandb_name, config=config)

  train(config, run)
