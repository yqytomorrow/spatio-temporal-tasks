import os
import argparse
import torch
import time
import pickle
import numpy as np

from torch import nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from dataset import VideoAnomalyDataset_C3D
from models import model

from tqdm import tqdm
from aggregate import remake_video_output, evaluate_auc, remake_video_3d_output
from aggregate import remake_video_output, evaluate_auc, remake_video_3d_output
from math import cos, pi
torch.backends.cudnn.benchmark = False

# Config
def get_configs():
    parser = argparse.ArgumentParser(description="Spatio-Temporal proxy task")
    parser.add_argument("--val_step", type=int, default=100)
    parser.add_argument("--print_interval", type=int, default=100)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--gpu_id", type=str, default=0)
    parser.add_argument("--log_date", type=str, default=None)
    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--static_threshold", type=float, default=0.2)
    parser.add_argument("--sample_num", type=int, default=7)
    parser.add_argument("--filter_ratio", type=float, default=0.5)
    parser.add_argument("--checkpoint", type=str, default=None)
    parser.add_argument("--dataset", type=str, default="ped2", choices=['shanghaitech', 'ped2', 'avenue'])
    args = parser.parse_args()

    args.device = torch.device("cuda:{}".format(args.gpu_id) if torch.cuda.is_available() else "cpu")
    if args.dataset in ['shanghaitech', 'avenue']:
        args.filter_ratio = 0.8
    elif args.dataset == 'ped2':
        args.filter_ratio = 0.5
    return args


def train(args):
    if not args.log_date:
        running_date = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
    else:
        running_date = args.log_date
    print("The running_data : {}".format(running_date))
    for k,v in vars(args).items():
        print("-------------{} : {}".format(k, v))

    # Load Data
    data_dir = f"./home/yqy/datasets/vad/{args.dataset}/training"
    detect_pkl = f'detect/{args.dataset}_train_detect_result_yolov3.pkl'

    vad_dataset = VideoAnomalyDataset_C3D(data_dir, 
                                          dataset=args.dataset,
                                          detect_dir=detect_pkl,
                                          fliter_ratio=args.filter_ratio, 
                                          frame_num=args.sample_num,
                                          static_threshold=args.static_threshold)

    vad_dataloader = DataLoader(vad_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0, pin_memory=True)
    net = model.WideBranchNet(time_length=args.sample_num, num_classes=[36, 4 * (args.sample_num - 2)])

    if args.checkpoint is not None:
        state = torch.load(args.checkpoint)
        print('load ' + args.checkpoint)
        net.load_state_dict(state, strict=True)
        net.cuda()
        smoothed_auc, smoothed_auc_avg, _ = val(args, net)
        exit(0)

    net.cuda(args.device)
    net = net.train()

    criterion = nn.CrossEntropyLoss(reduction='mean')
    optimizer = optim.Adam(params=net.parameters(), lr=1e-4, weight_decay=1e-2)

    # Train
    log_dir = './log/{}/'.format(running_date)
    writer = SummaryWriter(log_dir)

    t0 = time.time()
    global_step = 0

    max_acc = -1
    timestamp_in_max = None

    for epoch in range(args.epochs):

        for it, data in enumerate(vad_dataloader):
            video, obj, temp_labels, spat_labels,spat_piece_labels, t_flag = data['video'], data['obj'], data['label'], data["trans_label"], data['trans_label_piece'], data["temporal"]
            n_temp = t_flag.sum().item()

            obj = obj.cuda(args.device, non_blocking=True)
            temp_labels = temp_labels[t_flag].long().view(-1).cuda(args.device)

            spat_labels = spat_labels[~t_flag].long().view(-1).cuda(args.device)
            spat_piece_labels = spat_piece_labels[~t_flag].long().view(-1).cuda(args.device)

            spat_piece_logits, temp_logits = net(obj)
            spat_piece_logits = spat_piece_logits[~t_flag].view(-1, 4)
            temp_logits = temp_logits[t_flag].view(-1, 4)

            spat_piece_loss = criterion(spat_piece_logits, spat_piece_labels)
            temp_loss = criterion(temp_logits, temp_labels)

            loss = spat_piece_loss + temp_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            writer.add_scalar('Train/Loss', loss.item(), global_step=global_step)
            writer.add_scalar('Train/Spatial', spat_piece_loss.item(), global_step=global_step)
            writer.add_scalar('Train/temp', temp_loss.item(), global_step=global_step)

            if (global_step + 1) % args.print_interval == 0:
                print("[{}:{}/{}]\tloss: {:.6f} s_loss: {:.6f} t_loss: {:.6f} \ttime: {:.6f}".\
                        format(epoch, it + 1, len(vad_dataloader), loss.item(), spat_piece_loss.item(), temp_loss.item(),  time.time() - t0))
                t0 = time.time()

            global_step += 1

            if global_step % args.val_step == 0 and epoch >= 5:
                smoothed_auc, smoothed_auc_avg, temp_timestamp = val(args, net)
                writer.add_scalar('Test/smoothed_auc', smoothed_auc, global_step=global_step)
                writer.add_scalar('Test/smoothed_auc_avg', smoothed_auc_avg, global_step=global_step)

                if smoothed_auc > max_acc:
                    max_acc = smoothed_auc
                    timestamp_in_max = temp_timestamp
                    save = './checkpoint/{}_{}.pth'.format('best', running_date)
                    torch.save(net.state_dict(), save)

                print('cur max: ' + str(max_acc) + ' in ' + timestamp_in_max)
                net = net.train()
            

def val(args, net=None):
    if not args.log_date:
        running_date = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
    else:
        running_date = args.log_date
    print("The running_date : {}".format(running_date))

    # Load Data
    data_dir = f"./home/yqy/datasets/vad/{args.dataset}/testing"
    detect_pkl = f'detect/{args.dataset}_test_detect_result_yolov3.pkl'

    testing_dataset = VideoAnomalyDataset_C3D(data_dir, 
                                              dataset=args.dataset,
                                              detect_dir=detect_pkl,
                                              fliter_ratio=args.filter_ratio,
                                              frame_num=args.sample_num)
    testing_data_loader = DataLoader(testing_dataset, batch_size=192, shuffle=True, num_workers=0, drop_last=True)

    net.eval()

    video_output = {}
    for data in tqdm(testing_data_loader):
        videos = data["video"]
        frames = data["frame"].tolist()
        obj = data["obj"].cuda(args.device)
    
        with torch.no_grad():
            spat_piece_logits, temp_logits = net(obj)
            spat_piece_logits = spat_piece_logits.view(-1, 9, 4)
            temp_logits = temp_logits.view(-1, args.sample_num - 2, 4)

        spat_probs_piece = F.softmax(spat_piece_logits, -1)
        diag = spat_probs_piece[:, :, 0]
        scores = diag.min(-1)[0].cpu().numpy()

        temp_probs = F.softmax(temp_logits, -1)
        diag2 = temp_probs[:, :, -1]
        scores2 = diag2.min(-1)[0].cpu().numpy()


        
        for video_, frame_, s_score_, t_score_  in zip(videos, frames, scores, scores2):
            if video_ not in video_output:
                video_output[video_] = {}
            if frame_ not in video_output[video_]:
                video_output[video_][frame_] = []
            video_output[video_][frame_].append([s_score_, t_score_])

    micro_auc, macro_auc = save_and_evaluate(video_output, running_date, dataset=args.dataset)
    return micro_auc, macro_auc, running_date

def warmup_cosine(optimizer, current_epoch, max_epoch, lr_min=0, lr_max=0.1, warmup_epoch = 10):
    if current_epoch < warmup_epoch:
        lr = lr_max * current_epoch / warmup_epoch
    else:
        lr = lr = lr_min + (lr_max-lr_min)*(1 + cos(pi * (current_epoch - warmup_epoch) / (max_epoch - warmup_epoch))) / 2
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr

def save_and_evaluate(video_output, running_date, dataset='shanghaitech'):
    pickle_path = './log/video_output_ori_{}.pkl'.format(running_date)
    with open(pickle_path, 'wb') as write:
        pickle.dump(video_output, write, pickle.HIGHEST_PROTOCOL)
    if dataset == 'shanghaitech':
        video_output_spatial, video_output_temporal, video_output_complete = remake_video_output(video_output, dataset=dataset)
    else:
        video_output_spatial, video_output_temporal, video_output_complete = remake_video_3d_output(video_output, dataset=dataset)
    evaluate_auc(video_output_spatial, dataset=dataset)
    evaluate_auc(video_output_temporal, dataset=dataset)
    smoothed_res, smoothed_auc_list = evaluate_auc(video_output_complete, dataset=dataset)
    return smoothed_res.auc, np.mean(smoothed_auc_list)


if __name__ == '__main__':
    if not os.path.exists('checkpoint'):
        os.makedirs('checkpoint')
    args = get_configs()
    train(args)
