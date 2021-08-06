import torch.nn.functional as F
from mylib import compute_gradient_penalty, Multilabel_OneHot, KlLoss, visualizer, FocalLoss
from options import *
import numpy as np
import gc
import tqdm
import torch.autograd as autograd
from dataset import *
def gradient_penalty(netD, real, fake, res, batch_size, gamma=1):
    device = real.device
    alpha = torch.rand(batch_size, 1, 1, 1, requires_grad=True).to(device)
    x = alpha*real + (1-alpha)*fake
    d_= netD.forward(x, res)[0]
    g = torch.autograd.grad(outputs=d_, inputs=x,
                            grad_outputs=torch.ones(d_.shape).to(device),
                            create_graph=True, retain_graph=True,only_inputs=True)[0]
    g = g.reshape(batch_size, -1)
    return ((g.norm(2,dim=1)/gamma-1.0)**2).mean()

def pggan_train(param):
    # paramの変数
    G_model = param["G_model"]
    D_model = param["D_model"]
    dataset = param["dataset"]
    DataLoader = param["DataLoader"]
    label_weight = param['label_weight']
    pos_weight = param['pos_weight']
    device = param['device']
    ID = param['ID']
    char_num = param['char_num']
    test_z = param["z"]
    G_optimizer = param["G_optimizer"]
    D_optimizer = param["D_optimizer"]
    res_step = param['res_step']
    Tensor = param["Tensor"]
    latent_size = param['latent_size']
    iter_start = param["iter_start"]
    log_dir = param['log_dir']
    G_model_mavg = param["G_model_mavg"]
    writer = param['writer']
    ##training start
    G_model.train()
    D_model.train()
    iter = iter_start
    if iter == res_step * 6.5:
        G_optimizer.param_groups[0]['lr'] = 0.0001
        D_optimizer.param_groups[0]['lr'] = 0.0001
    #lossの初期化
    D_running_TF_loss = 0
    G_running_TF_loss = 0
    D_running_cl_loss = 0
    G_running_cl_loss = 0
    G_running_char_loss = 0
    D_running_char_loss = 0
    real_acc = []
    fake_acc = []
    #Dataloaderの定義
    databar = tqdm.tqdm(DataLoader)
    #バッチごとの計算
    criterion_pixel = torch.nn.L1Loss().to(device)
    f_loss = FocalLoss().to(device)
    #マルチクラス分類
    bce_loss = torch.nn.BCEWithLogitsLoss(weight=label_weight, pos_weight=pos_weight).to(device)
    kl_loss = KlLoss().to(device)
    mse_loss = torch.nn.MSELoss().to(device)

    for batch_idx, samples in enumerate(databar):
        real_img, char_class, labels = samples['img_target'], samples['charclass_target'], samples['multi_embed_label_target']
        #ステップの定義
        res = iter / res_step
        # get integer by floor
        #image size define
        eps = 1e-7
        n = min(res, len(G_model.module.blocks))
        n_layer = max(int(n - eps), 0)
        img_size = G_model.module.size[n_layer]
        real_img = F.adaptive_avg_pool2d(real_img, (img_size, img_size))

        # バッチの長さを定義
        batch_len = real_img.size(0)
        #デバイスの移
        real_img,  char_class = \
            real_img.to(device), char_class.to(device)
        # 文字クラスのone-hotベクトル化
        char_class_oh = torch.eye(char_num)[char_class].to(device)
        # 印象語のベクトル化
        labels_oh = Multilabel_OneHot(labels, len(ID), normalize=False).to(device)
        # training Generator
        #画像の生成に必要なノイズ作成
        z = torch.randn(batch_len, latent_size * 16)
        ##画像の生成に必要な印象語ラベルを取得
        _, _, D_real_class = D_model(real_img, res)
        gen_label = F.sigmoid(D_real_class.detach())
        # ２つのノイズの結合
        fake_img, mu, logvar = G_model(z, char_class_oh, gen_label, res)
        D_fake_TF, D_fake_char, D_fake_class = D_model(fake_img, res)
        # Wasserstein lossの計算
        G_TF_loss = -torch.mean(D_fake_TF)
        # 文字クラス分類のロス
        G_char_loss = kl_loss(F.log_softmax(D_fake_char, dim=1), char_class_oh)
        # 印象語分類のロス
        G_class_loss = mse_loss(F.sigmoid(D_fake_class), gen_label)
        # CAにおける損失
        # CA_loss = kl_loss(mu, logvar)
        G_loss = G_TF_loss + G_char_loss + G_class_loss \
                 # + CA_loss
        G_optimizer.zero_grad()
        G_loss.backward()
        G_optimizer.step()
        G_running_TF_loss += G_TF_loss.item()
        G_running_cl_loss += G_class_loss.item()
        G_running_char_loss += G_char_loss.item()

        # update netG_mavg by moving average
        momentum = 0.995  # remain momentum
        alpha = min(1.0 - (1 / (iter + 1)), momentum)
        for p_mavg, p in zip(G_model_mavg.parameters(), G_model.parameters()):
            p_mavg.data = alpha * p_mavg.data + (1.0 - alpha) * p.data

        #training Discriminator
        #Discriminatorに本物画像を入れて順伝播⇒Loss計算
        for _ in range(1):
            D_real_TF,  D_real_char, D_real_class = D_model(real_img, res)
            # 生成用のラベル
            gen_label = F.sigmoid(D_real_class.detach())
            D_real_loss = -torch.mean(D_real_TF)
            fake_img, _, _ = G_model(z, char_class_oh, gen_label, res)
            D_fake_TF = D_model(fake_img.detach(), res)[0]
            D_fake_loss = torch.mean(D_fake_TF)
            gp_loss = gradient_penalty(D_model, real_img.data, fake_img.data, res, real_img.shape[0])
            loss_drift = (D_real_TF ** 2).mean()

            #Wasserstein lossの計算
            D_TF_loss = D_fake_loss + D_real_loss + 10 * gp_loss
            # 文字クラス分類のロス
            D_char_loss = kl_loss(F.log_softmax(D_real_char, dim=1), char_class_oh)
            # 印象語分類のロス
            D_class_loss = bce_loss(D_real_class, labels_oh)
            D_loss = D_TF_loss + D_char_loss + loss_drift * 0.001 + D_class_loss
            D_optimizer.zero_grad()
            D_loss.backward()
            D_optimizer.step()
            D_running_TF_loss += D_TF_loss.item()
            D_running_cl_loss += D_class_loss.item()
            D_running_char_loss += D_char_loss.item()

        ##caliculate accuracy
        real_pred = 1 * (torch.sigmoid(D_real_TF) > 0.5).detach().cpu()
        fake_pred = 1 * (torch.sigmoid(D_fake_TF) > 0.5).detach().cpu()
        real_TF = torch.ones(real_pred.size(0))
        fake_TF = torch.zeros(fake_pred.size(0))
        r_acc = (real_pred == real_TF).float().sum().item()/len(real_pred)
        f_acc = (fake_pred == fake_TF).float().sum().item()/len(fake_pred)
        real_acc.append(r_acc)
        fake_acc.append(f_acc)

        ##tensor bord
        writer.add_scalars("TF_loss", {'D_TF_loss': D_TF_loss, 'G_TF_loss': G_TF_loss}, iter)
        writer.add_scalars("class_loss", {'D_class_loss': D_class_loss, 'G_class_loss': G_class_loss}, iter)
        writer.add_scalars("char_loss", {'D_char_loss': D_char_loss, 'G_char_loss': G_char_loss}, iter)
        writer.add_scalars("Acc", {'real_acc': r_acc, 'fake_acc': f_acc}, iter)

        iter += 1

        if iter % 250 == 0:
            test_label = ['decorative', 'big', 'shading', 'manuscript', 'ghost']
            test_emb_label = [[ID[key]] for key in test_label]
            label = Multilabel_OneHot(test_emb_label, len(ID), normalize=False)
            save_path = os.path.join(log_dir, 'img_iter_%05d_%02d✕%02d.png' % (iter, real_img.size(2), real_img.size(3)))
            visualizer(save_path, G_model_mavg, test_z, char_num, label, res, device)
            G_model_mavg.train()

        if iter >= res_step * 7:
            break

    D_running_TF_loss /= len(DataLoader)
    G_running_TF_loss /= len(DataLoader)
    D_running_cl_loss /= len(DataLoader)
    G_running_cl_loss /= len(DataLoader)
    D_running_char_loss /= len(DataLoader)
    G_running_char_loss /= len(DataLoader)
    real_acc = sum(real_acc)/len(real_acc)
    fake_acc = sum(fake_acc)/len(fake_acc)
    check_point = {'G_net': G_model.state_dict(),
                   'G_optimizer': G_optimizer.state_dict(),
                   'D_net': D_model.state_dict(),
                   'D_optimizer': D_optimizer.state_dict(),
                   'D_epoch_TF_losses': D_running_TF_loss,
                   'G_epoch_TF_losses': G_running_TF_loss,
                   'D_epoch_cl_losses': D_running_cl_loss,
                   'G_epoch_cl_losses': G_running_cl_loss,
                   'D_epoch_ch_losses': D_running_char_loss,
                   'G_epoch_ch_losses': G_running_char_loss,
                   'epoch_real_acc': real_acc,
                  'epoch_fake_acc':fake_acc,
                   "iter_finish":iter,
                   "res":res
                   }
    return check_point