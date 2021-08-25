from trainer.train import pggan_train
# from models.DCmodel import ACGenerator, ACDiscriminator, CGenerator, CDiscriminator
from mylib import *
from dataset import *
from torchvision.utils import make_grid, save_image
from models.PGmodel import Generator, Discriminator
from torchinfo import summary
from torch.utils.tensorboard import SummaryWriter
from torch.autograd import detect_anomaly
def pgmodel_run(opts):
    #各種必要なディレクトリの作成
    log_dir = os.path.join(opts.root, opts.dt_now)
    check_point_dir = os.path.join(log_dir, 'check_points')
    output_dir = os.path.join(log_dir, 'output')
    weight_dir = os.path.join(log_dir, 'weight')
    logs_GAN = os.path.join(log_dir, "learning_image")
    learning_log_dir = os.path.join(log_dir, "learning_log")
    if not os.path.exists(log_dir):
        os.makedirs(check_point_dir)
    if not os.path.exists(check_point_dir):
        os.makedirs(check_point_dir)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    if not os.path.exists(weight_dir):
        os.makedirs(weight_dir)
    if not os.path.exists(logs_GAN):
        os.makedirs(logs_GAN)
    if not os.path.exists(learning_log_dir):
        os.makedirs(learning_log_dir)
    data = np.array([np.load(d) for d in opts.data])
    # 生成に必要な乱数
    z_img = torch.randn(4, opts.latent_size * 16)
    z_cond = torch.randn(4, opts.num_dimension)
    z = (z_img, z_cond)
    #単語IDの変換
    ID = {key:idx+1 for idx, key in enumerate(opts.w2v_vocab)}
    weights = np.array(list(opts.w2v_vocab.values()))
    imp_num = weights.shape[0]
    w2v_dimension = weights.shape[1]
    #モデルを定義
    D_model = Discriminator(imp_num=imp_num, char_num=opts.char_num).to(opts.device)
    G_model = Generator(weights, latent_size=opts.latent_size, w2v_dimension=w2v_dimension, num_dimension=opts.num_dimension, char_num=opts.char_num).to(opts.device)
    G_model_mavg = Generator(weights, latent_size=opts.latent_size, w2v_dimension=w2v_dimension, num_dimension=opts.num_dimension, char_num=opts.char_num).to(opts.device)
    #学習済みモデルのパラメータを使用
    # GPUの分散
    if opts.device_count > 1:
        D_model = nn.DataParallel(D_model)
        G_model = nn.DataParallel(G_model)
        G_model_mavg = nn.DataParallel(G_model_mavg)

    # optimizerの定義
    D_optimizer = torch.optim.Adam(D_model.parameters(), lr=opts.g_lr, betas=(0, 0.99))
    G_optimizer = torch.optim.Adam(G_model.parameters(), lr=opts.g_lr, betas=(0, 0.99))

    D_TF_loss_list = []
    G_TF_loss_list = []
    D_ch_loss_list = []
    G_ch_loss_list = []
    D_cl_loss_list = []
    G_cl_loss_list = []
    real_acc_list = []
    fake_acc_list = []

    transform = Transform()
    #training param
    iter_start = 0
    writer = SummaryWriter(log_dir=learning_log_dir)
    for epoch in range(opts.num_epochs):
        start_time = time.time()

        dataset = Myfont_dataset3(data, opts.impression_word_list, ID, char_num=opts.char_num,
                                  transform=transform)
        bs = opts.batch_size
        label_weight = 1/dataset.weight
        pos_weight = (dataset.weight.sum() - dataset.weight)/dataset.weight

        DataLoader = torch.utils.data.DataLoader(dataset, batch_size=bs, shuffle=True,
                                                 collate_fn=collate_fn, drop_last=True)

        param = {"opts":opts, 'epoch': epoch, 'G_model': G_model, 'D_model': D_model,
                 'G_model_mavg':G_model_mavg, "dataset":dataset, "z":z, "label_weight":label_weight, 'pos_weight':pos_weight,
                 'DataLoader': DataLoader,
                 'G_optimizer': G_optimizer, 'D_optimizer': D_optimizer, 'log_dir':logs_GAN, "iter_start":iter_start,'ID': ID, 'writer': writer}
        check_point = pggan_train(param)
        iter_start = check_point["iter_finish"]+1
        D_TF_loss_list.append(check_point["D_epoch_TF_losses"])
        G_TF_loss_list.append(check_point["G_epoch_TF_losses"])
        D_cl_loss_list.append(check_point["D_epoch_cl_losses"])
        G_cl_loss_list.append(check_point["G_epoch_cl_losses"])
        D_ch_loss_list.append(check_point["D_epoch_ch_losses"])
        G_ch_loss_list.append(check_point["G_epoch_ch_losses"])
        real_acc_list.append(check_point["epoch_real_acc"])
        fake_acc_list.append(check_point["epoch_fake_acc"])

        history = {'D_TF_loss': D_TF_loss_list,
         'G_TF_loss': G_TF_loss_list,
         'D_class_loss': D_cl_loss_list,
         'G_class_loss': G_cl_loss_list,
         'D_char_loss': D_ch_loss_list,
         'G_char_loss': G_ch_loss_list
                   }

        accuracy = {'real_acc':real_acc_list,
                    'fake_acc':fake_acc_list}

        secs = int(time.time() - start_time)
        mins = secs / 60
        secs = secs % 60

        # エポックごとに結果を表示
        print('Epoch: %d' % (epoch + 1), " | 所要時間 %d 分 %d 秒" % (mins, secs))
        print(f'\tLoss: {check_point["D_epoch_TF_losses"]:.4f}(Discriminator_TF)')
        print(f'\tLoss: {check_point["G_epoch_TF_losses"]:.4f}(Generator_TF)')
        print(f'\tLoss: {check_point["D_epoch_cl_losses"]:.4f}(Discriminator_class)')
        print(f'\tLoss: {check_point["G_epoch_cl_losses"]:.4f}(Generator_class)')
        print(f'\tLoss: {check_point["D_epoch_ch_losses"]:.4f}(Discriminator_char)')
        print(f'\tLoss: {check_point["G_epoch_ch_losses"]:.4f}(Generator_char)')
        print(f'\tacc: {check_point["epoch_real_acc"]:.4f}(real_acc)')
        print(f'\tacc: {check_point["epoch_fake_acc"]:.4f}(fake_acc)')
        if (epoch + 1) % 1 == 0:
            learning_curve(history, os.path.join(learning_log_dir, 'loss_epoch_{}'.format(epoch + 1)))
            learning_curve(accuracy, os.path.join(learning_log_dir, 'acc_epoch_{}'.format(epoch + 1)))
        # モデル保存のためのcheckpointファイルを作成
        if (epoch + 1) % 5 == 0:
            torch.save(check_point, os.path.join(check_point_dir, 'check_point_epoch_%d.pth' % (epoch)))

        if iter_start >= opts.res_step*7:
            break

    writer.close()
    return D_TF_loss_list, G_TF_loss_list, D_cl_loss_list, G_cl_loss_list

def main():
    parser = get_parser()
    opts = parser.parse_args()
    # 再現性確保のためseed値固定
    SEED = 42
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed(SEED)
    torch.backends.cudnn.deterministic = True
    #回すモデルの選定
    pgmodel_run(opts)

if __name__=="__main__":
    main()
