import argparse
import os
import statistics

import torch
import tqdm
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter



import sys
sys.path.append('/home/jinlongli/1.Detection_Set/V2V_Attack')
sys.path.append('/home/jinlongli/1.Detection_Set/V2V_Attack/opencood')

# sys.path.append("/home/jinlong/4.3D_detection/Noise_V2V/v2vreal")
# import sys
# import os
# curPath = os.path.abspath(os.path.dirname(__file__))
# rootPath = os.path.split(curPath)[0]
# sys.path.append(rootPath)
# sys.path.remove("/home/jinlongli/1.Detection_Set/DA_V2V")
print(sys.path)

import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils
from opencood.data_utils.datasets import build_dataset



def train_parser():
    parser = argparse.ArgumentParser(description="synthetic data generation")
    parser.add_argument("--hypes_yaml", type=str,default="/home/jinlongli/1.Detection_Set/V2V_Attack/opencood/hypes_yaml/point_pillar_intermediate_V2VAM.yaml",#
                        help='data generation yaml file needed ')
    parser.add_argument('--model_dir', default="", #home/jinlongli/2.model_saved/3.Attack_V2V2023/0.attack_model/V2VAM_finetune_voxel_2023_07_21_07
                        help='Continued training path')
    parser.add_argument('--model', default='',
                        help='for fine-tuned training path')
    parser.add_argument("--half", action='store_true', help="whether train with half precision")
    opt = parser.parse_args()
    return opt


def main():
    opt = train_parser()
    hypes = yaml_utils.load_yaml(opt.hypes_yaml, opt)

    print('Dataset Building')
    opencood_train_dataset = build_dataset(hypes, visualize=False, train=True,isSim=True)
    opencood_validate_dataset = build_dataset(hypes,
                                              visualize=False,
                                              train=False,
                                              isSim=True)

    train_loader = DataLoader(opencood_train_dataset,
                              batch_size=hypes['train_params']['batch_size'],
                              num_workers=8,
                              collate_fn=opencood_train_dataset.collate_batch_train,
                              shuffle=True,
                              pin_memory=False,
                              drop_last=True)
    val_loader = DataLoader(opencood_validate_dataset,
                            batch_size=hypes['train_params']['batch_size'],
                            num_workers=8,
                            collate_fn=opencood_train_dataset.collate_batch_train,
                            shuffle=False,
                            pin_memory=False,
                            drop_last=True)

    print('Creating Model')
    model = train_utils.create_model(hypes)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # we assume gpu is necessary
    if torch.cuda.is_available():
        model.to(device)

    # define the loss
    criterion = train_utils.create_loss(hypes)

    # optimizer setup
    optimizer = train_utils.setup_optimizer(hypes, model)
    # lr scheduler setup
    num_steps = len(train_loader)
    scheduler = train_utils.setup_lr_schedular(hypes, optimizer, num_steps)

   # if we want to train from last checkpoint.
    if opt.model_dir:
        saved_path = opt.model_dir
        init_epoch, model = train_utils.load_saved_model(saved_path, model)
        print('Loaded model from {}'.format(saved_path))

    else:
        if opt.model:
            saved_path = train_utils.setup_train(hypes)
            model_path = opt.model
            init_epoch = 0
            # pretrained_state = torch.load(os.path.join(model_path,'latest.pth'))
            pretrained_state = torch.load(model_path)
            model_dict = model.state_dict()
            pretrained_state = {k: v for k, v in pretrained_state.items() if (k in model_dict and v.shape == model_dict[k].shape)}
            model_dict.update(pretrained_state)
            model.load_state_dict(model_dict)
            print('Loaded pretrained model from {}'.format(model_path))
        else:
            init_epoch = 0
            # if we train the model from scratch, we need to create a folder
            # to save the model,
            saved_path = train_utils.setup_train(hypes)

    # record training
    writer = SummaryWriter(saved_path)

    # half precision training
    if opt.half:
        scaler = torch.cuda.amp.GradScaler()

    print('Training start')
    txt_path = os.path.join(saved_path, 'training_eval_log.txt')
    txt_log = open(txt_path, "w")
    epoches = hypes['train_params']['epoches']
    print('epoches:  '+str(epoches))
    # used to help schedule learning rate
    for epoch in range(init_epoch, max(epoches, init_epoch)):
        if hypes['lr_scheduler']['core_method'] != 'cosineannealwarm':
            scheduler.step(epoch)
        for param_group in optimizer.param_groups:
            print('learning rate %f' % param_group["lr"])
        pbar2 = tqdm.tqdm(total=len(train_loader), leave=True)
        for i, batch_data in enumerate(train_loader):
            # the model will be evaluation mode during validation
            model.train()
            model.zero_grad()
            optimizer.zero_grad()

            ###baolu
            batch_data['ego'].pop('base_data_dict')
            batch_data['ego'].pop('transformation_matrix')
            batch_data['ego'].pop('processered_lidar_np')
            
            batch_data = train_utils.to_device(batch_data, device)

            # case1 : late fusion train --> only ego needed
            # case2 : early fusion train --> all data projected to ego
            # case3 : intermediate fusion --> ['ego']['processed_lidar']
            # becomes a list, which containing all data from other cavs
            # as well
            if not opt.half:
                ouput_dict = model(batch_data['ego'])
                # first argument is always your output dictionary,
                # second argument is always your label dictionary.
                final_loss = criterion(ouput_dict, batch_data['ego']['label_dict'])
            else:
                with torch.cuda.amp.autocast():
                    ouput_dict = model(batch_data['ego'])
                    final_loss = criterion(ouput_dict, batch_data['ego']['label_dict'])

            criterion.logging(epoch, i, len(train_loader), writer, pbar=pbar2)
            pbar2.update(1)
            # back-propagation
            if not opt.half:
                final_loss.backward()
                optimizer.step()
            else:
                scaler.scale(final_loss).backward()
                scaler.step(optimizer)
                scaler.update()

            if hypes['lr_scheduler']['core_method'] == 'cosineannealwarm':
                scheduler.step_update(epoch * num_steps + i)

        if epoch % hypes['train_params']['eval_freq'] == 0:
            valid_ave_loss = []

            with torch.no_grad():
                for i, batch_data in enumerate(val_loader):
                    model.eval()
                    
                    batch_data['ego'].pop('base_data_dict')
                    batch_data['ego'].pop('transformation_matrix')
                    batch_data['ego'].pop('processered_lidar_np')
                    batch_data = train_utils.to_device(batch_data, device)
                    ouput_dict = model(batch_data['ego'])

                    final_loss = criterion(ouput_dict,
                                           batch_data['ego']['label_dict'])
                    valid_ave_loss.append(final_loss.item())
            valid_ave_loss = statistics.mean(valid_ave_loss)
            print('At epoch %d, the validation loss is %f' % (epoch,
                                                               valid_ave_loss))
            txt_log.write('At epoch ' + str(epoch+1)+',  the validation loss is '+ str(valid_ave_loss) + ' save in '+ str(os.path.join(saved_path,'net_epoch%d.pth' % (epoch + 1))) + '\n')

            writer.add_scalar('Validate_Loss', valid_ave_loss, epoch)

        if epoch % hypes['train_params']['save_freq'] == 0:
            torch.save(model.state_dict(),
                       os.path.join(saved_path,
                                    'net_epoch%d.pth' % (epoch + 1)))

        opencood_train_dataset.reinitialize()

    print('Training Finished, checkpoints saved to %s' % saved_path)
    # close file
    txt_log.close()


if __name__ == '__main__':
    main()
