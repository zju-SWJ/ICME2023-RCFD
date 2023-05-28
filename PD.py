import copy
import os
import warnings
from absl import app, flags

import torch
from tensorboardX import SummaryWriter
from torchvision.datasets import CIFAR10
from torchvision.utils import make_grid, save_image
from torchvision import transforms
from tqdm import trange
import torch.distributed as dist
from torchvision.datasets import ImageFolder
from diffusion import GaussianDiffusionSampler
from model import UNet

FLAGS = flags.FLAGS
flags.DEFINE_enum('dataset', 'cifar10', ['cifar10', 'imagenet64'], help='dataset')
# UNet
flags.DEFINE_integer('ch', 128, help='base channel of UNet')
flags.DEFINE_multi_integer('ch_mult', [1, 2, 2, 2], help='channel multiplier')
flags.DEFINE_multi_integer('attn', [1], help='add attention to these levels')
flags.DEFINE_integer('num_res_blocks', 2, help='# resblock in each level')
flags.DEFINE_float('dropout', 0., help='dropout rate of resblock')
# Gaussian Diffusion
flags.DEFINE_enum('mean_type', 'xstart', ['xstart', 'epsilon'], help='predict variable')
flags.DEFINE_enum('var_type', 'fixedlarge', ['fixedlarge', 'fixedsmall'], help='variance type')
# Training
flags.DEFINE_float('lr', 5e-5, help='target learning rate')
flags.DEFINE_float('wd', 0., help='target learning rate')
flags.DEFINE_float('grad_clip', 1., help="gradient norm clipping")
flags.DEFINE_integer('total_steps', 10000, help='total training steps') # 2x steps are used when distilling 1-step and 2-step students
flags.DEFINE_integer('img_size', 32, help='image size')
flags.DEFINE_integer('warmup', 0, help='learning rate warmup')
flags.DEFINE_integer('batch_size', 128, help='batch size')
flags.DEFINE_integer('num_workers', 4, help='workers of Dataloader')
flags.DEFINE_float('ema_decay', 0., help="ema decay rate")
flags.DEFINE_string('gpu_id', '4,5,6,7', help='multi gpu training')
flags.DEFINE_integer('local_rank', 0, help='local rank')
flags.DEFINE_bool('distributed', False, help='multi gpu training')
flags.DEFINE_integer('num_gpus', 4, help='multi gpu training')
flags.DEFINE_bool('conditional', False, help='use conditional or not')
flags.DEFINE_integer('class_num', 10, help='class num')
# Logging & Sampling
flags.DEFINE_string('logdir', './logs/CIFAR10/4', help='log directory')
flags.DEFINE_string('base_ckpt', './logs/CIFAR10/8', help='base ckpt')
flags.DEFINE_integer('sample_size', 64, "sampling size of images")
flags.DEFINE_integer('sample_step', 1000, help='frequency of sampling')
flags.DEFINE_integer('save_step', 1000, help='frequency of saving checkpoints, 0 to disable during training')
flags.DEFINE_integer('seed', 0, help='seed')

def infiniteloop(dataloader):
    while True:
        for x, y in iter(dataloader):
            yield x, y

def get_rank():
    if not dist.is_available():
        return 0
    if not dist.is_initialized():
        return 0
    return dist.get_rank()

def train():
    if get_rank() == 0:
        if not os.path.exists(os.path.join(FLAGS.logdir, 'ddim_clip')):
            os.makedirs(os.path.join(FLAGS.logdir, 'ddim_clip'))
    ckpt_teacher = torch.load(os.path.join(FLAGS.base_ckpt, 'ckpt.pt'), map_location='cuda:{}'.format(FLAGS.local_rank))
    T = ckpt_teacher['T']
    time_scale = ckpt_teacher['time_scale']

    # dataset
    if FLAGS.dataset == 'cifar10':
        dataset = CIFAR10(
            root='../../data', train=True, download=True,
            transform=transforms.Compose([
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            ]))
    elif FLAGS.dataset == 'imagenet64':
        dataset = ImageFolder(
            '../../data/ImageNet64/train',
            transforms.Compose([
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            ]))

    # model setup
    model = UNet(
        T=T*time_scale, ch=FLAGS.ch, ch_mult=FLAGS.ch_mult, attn=FLAGS.attn,
        num_res_blocks=FLAGS.num_res_blocks, dropout=FLAGS.dropout,
        conditional=FLAGS.conditional, class_num=FLAGS.class_num)
    if time_scale == 1:
        model.load_state_dict(ckpt_teacher['ema_model'])
    else:
        model.load_state_dict(ckpt_teacher['net_model'])
    student_model = copy.deepcopy(model)
    
    teacher_sampler = GaussianDiffusionSampler(
        model, T, time_scale, img_size=FLAGS.img_size,
        mean_type=FLAGS.mean_type, var_type=FLAGS.var_type).cuda(FLAGS.local_rank)
    student_sampler = GaussianDiffusionSampler(
        student_model, T // 2, time_scale * 2, img_size=FLAGS.img_size,
        mean_type=FLAGS.mean_type, var_type=FLAGS.var_type).cuda(FLAGS.local_rank)
    if FLAGS.distributed:
        teacher_sampler = torch.nn.parallel.DistributedDataParallel(teacher_sampler, device_ids=[FLAGS.local_rank], output_device=FLAGS.local_rank)
        student_sampler = torch.nn.parallel.DistributedDataParallel(student_sampler, device_ids=[FLAGS.local_rank], output_device=FLAGS.local_rank)
    
    optim = torch.optim.Adam(student_sampler.parameters(), lr=FLAGS.lr, weight_decay=FLAGS.wd)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(optim, T_max=FLAGS.total_steps)
    
    batch_size = int(FLAGS.batch_size / FLAGS.num_gpus)
    train_sampler = torch.utils.data.distributed.DistributedSampler(dataset, seed=FLAGS.seed)
    train_loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, num_workers=FLAGS.num_workers, pin_memory=True, sampler=train_sampler, drop_last=True)
    train_looper = infiniteloop(train_loader)
    # log setup
    x_T = ckpt_teacher['x_T']
    del ckpt_teacher
    grid = (make_grid(next(iter(train_loader))[0][:int(FLAGS.sample_size / FLAGS.num_gpus)]) + 1) / 2
    if get_rank() == 0:
        writer = SummaryWriter(FLAGS.logdir)
        writer.add_image('real_sample', grid)
        writer.flush()
        # backup all arguments
        with open(os.path.join(FLAGS.logdir, "flagfile.txt"), 'w') as f:
            f.write(FLAGS.flags_into_string())

    # start training
    teacher_sampler.eval()
    for p in teacher_sampler.parameters():
        p.requires_grad_(False)

    for step in range(1, FLAGS.total_steps + 1):
        train_sampler.set_epoch(step)
        # train
        samples = next(train_looper)
        x_0, y = samples[0].cuda(FLAGS.local_rank), samples[1].cuda(FLAGS.local_rank)
        loss = teacher_sampler.module.PD(student_sampler, x_0, y)
        optim.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(student_sampler.parameters(), FLAGS.grad_clip)
        optim.step()
        sched.step()

        # log
        if get_rank() == 0:
            writer.add_scalar('loss', loss, step)

        # sample
        if FLAGS.sample_step > 0 and (step % FLAGS.sample_step == 0 or step == 1):
            student_sampler.eval()
            with torch.no_grad():
                y_target = torch.randint(FLAGS.class_num, size=(x_T.shape[0],), device=x_T.device)
                x_0 = student_sampler.module.ddim(x_T, 1, True, y=y_target)
                grid = (make_grid(x_0) + 1) / 2
                path = os.path.join(FLAGS.logdir, 'ddim_clip', '%d.png' % step)
                if get_rank() == 0:
                    save_image(grid, path)
                    writer.add_image('ddim_clip', grid, step)
            student_sampler.train()

        # save
        if FLAGS.save_step > 0 and step % FLAGS.save_step == 0:
            if get_rank() == 0:
                ckpt_student = {
                    'net_model': student_sampler.module.model.state_dict(),
                    'x_T': x_T,
                    'T': student_sampler.module.T,
                    'time_scale': student_sampler.module.time_scale,
                }
                torch.save(ckpt_student, os.path.join(FLAGS.logdir, 'ckpt.pt'))
    torch.distributed.barrier()
    if get_rank() == 0:
        writer.close()


def main(argv):
    os.environ["CUDA_VISIBLE_DEVICES"] = FLAGS.gpu_id
    seed = FLAGS.seed
    os.environ['PYTHONHASHSEED'] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    # Not fully deterministic
    FLAGS.num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    FLAGS.distributed = FLAGS.num_gpus > 1
    if FLAGS.distributed:
        torch.cuda.set_device(FLAGS.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
    # suppress annoying inception_v3 initialization warning
    warnings.simplefilter(action='ignore', category=FutureWarning)
    train()


if __name__ == '__main__':
    app.run(main)
