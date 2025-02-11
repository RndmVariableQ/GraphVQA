"""
Miscellaneous functions that might be useful for pytorch
"""

import random
import h5py
import numpy as np
import torch
from torch.autograd import Variable
import os
import dill as pkl
from itertools import tee
from torch import optim
from torchvision.ops.boxes import box_iou


def optimistic_restore(network, state_dict, names_map=None):
    mismatch = False
    only_detector = False
    own_state = network.state_dict()
    state_dict_keys_new = set()
    for name_, param in state_dict.items():
        if names_map is not None:
            for name_old, name_new in names_map.items():
                name_ = name_.replace(name_old, name_new)
        state_dict_keys_new.add(name_)
        name2 = 'detector.' + name_
        if name2 in own_state and name_ not in own_state:
            name = name2
            only_detector = True
        else:
            name = name_
            if only_detector:
                print('could not restore')
                mismatch = True

            only_detector = False

        if name not in own_state:
            print("Unexpected key {} in state_dict with size {}".format(name, param.size()))
            mismatch = True
        elif param.size() == own_state[name].size():
            own_state[name].copy_(param)
        else:
            print("Network has {} with size {}, ckpt has {}".format(name,
                                                                    own_state[name].size(),
                                                                    param.size()))
            mismatch = True

    if only_detector:
        print('detector restored - {} success ({} keys)'.format('NOT' if mismatch else '', len(state_dict.items())))
    else:
        missing = set(own_state.keys()) - state_dict_keys_new # set(state_dict.keys())
        if len(missing) > 0:
            print("We couldn't find {}".format(','.join(missing)))
            mismatch = True
    return not mismatch


def bbox_overlaps(boxes1, boxes2):
    is_np = isinstance(boxes1, np.ndarray)
    if is_np:
        boxes1, boxes2 = torch.from_numpy(boxes1), torch.from_numpy(boxes2)
    iou =  box_iou(boxes1.float(), boxes2.float())
    if is_np:
        return iou.numpy()
    return iou


def grad_clip(detector, clip, verbose):
    clip_grad_norm(
        [(n, p) for n, p in detector.named_parameters() if (p.grad is not None)],
        max_norm=clip, verbose=verbose, clip=True)


def set_mode(sgg_model, mode, is_train, verbose=False):
    if is_train:
        sgg_model.train()
    else:
        sgg_model.eval()

    sgg_model.mode = mode

    if hasattr(sgg_model, 'detector'):
        m = 'refinerels' if mode == 'sgdet' else 'gtbox'
        if verbose:
            print('setting %s mode for detector' % m)
        sgg_model.detector.mode = m
        # if sgg_model.backbone != 'vgg16_old':
        #     sgg_model.detector.eval()  # Assume the detector is never trained

    if hasattr(sgg_model, 'context'):
        if verbose:
            print('setting %s mode for context' % mode)
        sgg_model.context.mode = mode


def get_optim_gan(gan, conf, start_epoch, ckpt=None):

    G_params = [(n, p) for n, p in gan.named_parameters() if n.startswith('G_') and p.requires_grad]
    D_params = [(n, p) for n, p in gan.named_parameters() if n.startswith('D_') and p.requires_grad]
    n_g = np.sum([np.prod(p[1].shape) for p in G_params])
    n_d = np.sum([np.prod(p[1].shape) for p in D_params])
    n_all = np.sum([np.prod(p.shape) for n, p in gan.named_parameters()])
    print('\nG params total:', n_g)
    print('D params total:', n_d)
    print('All GAN params total:', n_all)
    if n_g + n_d != n_all:
        print('WARNING: some parameters are not trained')

    # Use a separate optimizer for the generator:
    # https://github.com/znxlwm/pytorch-generative-model-collections/blob/master/GAN.py
    G_optimizer = optim.Adam([p[1] for p in G_params], lr=conf.lrG, betas=(conf.beta1, conf.beta2))
    D_optimizer = optim.Adam([p[1] for p in D_params], lr=conf.lrD, betas=(conf.beta1, conf.beta2))

    if start_epoch > -1 and ckpt is not None:
        print("Restoring GAN optimizers")
        try:
            G_optimizer.load_state_dict(ckpt['G_optimizer'])
        except Exception as e:
            print('error restoring G_optimizer', e)
        try:
            D_optimizer.load_state_dict(ckpt['D_optimizer'])
        except Exception as e:
            print('error restoring D_optimizer', e)

    return G_optimizer, D_optimizer


def get_optim(detector, lr, conf, start_epoch, ckpt=None):
    print('\nEffective learning rate is %.3e' % lr)

    # Lower the learning rate on the VGG fully connected layers by 1/10th. It's a hack, but it helps
    # stabilize the models.
    fc_params = [(n,p) for n,p in detector.named_parameters() if n.startswith('roi_fmap') and p.requires_grad]
    non_fc_params = [(n,p) for n, p in detector.named_parameters() if not n.startswith('roi_fmap') and p.requires_grad]

    # print('fc_params', [p[0] for p in fc_params])
    # print('non_fc_params', [p[0] for p in non_fc_params])

    params = [{'params': [p[1] for p in fc_params], 'lr': lr / 10.0 },
              {'params': [p[1] for p in non_fc_params]}]

    optimizer = optim.SGD(params, weight_decay=conf.l2, lr=lr, momentum=0.9)

    if start_epoch > -1 and ckpt is not None:
        print("Restoring optimizers")
        try:
            optimizer.load_state_dict(ckpt['optimizer'])
        except Exception as e:
            print('error restoring optimizer', e)

    scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                               milestones=[s + 1 for s in conf.steps],  # +1 for consistency with the paper
                                               gamma=conf.lr_decay)

    return optimizer, scheduler


def load_checkpoint(conf, detector, checkpoint_path=None, gan=None):
    start_epoch, ckpt = -1, None
    detector.global_batch_iter = 0  # for wandb

    checkpoint_path_load = checkpoint_path if (checkpoint_path is not None and os.path.exists(checkpoint_path)) \
        else (conf.ckpt if len(conf.ckpt) > 0 else None)

    if checkpoint_path_load is not None:
        print("\nLoading EVERYTHING from %s" % checkpoint_path_load)
        ckpt = torch.load(checkpoint_path_load, map_location=conf.device)

        if False: #os.path.basename(checkpoint_path_load).find('vgrel') >= 0:
            # If there's already a checkpoint in the save_dir path, assume we should load it and continue
            start_epoch = ckpt['epoch']
            if not optimistic_restore(detector, ckpt['state_dict']):
                start_epoch = -1
            else:
                detector.global_batch_iter = ckpt['global_batch_iter']
                if conf.gan:
                    assert 'gan' in ckpt, list(ckpt.keys())
                    gan.load_state_dict(ckpt['gan'])
                    print('GAN loaded successfully')

        elif conf.backbone.startswith('vgg16'):
            names_map = {}
            if conf.backbone == 'vgg16':
                names_map = {'features.': 'backbone.',
                             'roi_fmap.0': 'roi_heads.box_head.fc6',
                             'roi_fmap.3': 'roi_heads.box_head.fc7',
                             'score_fc': 'roi_heads.box_predictor.cls_score',
                             'bbox_fc': 'roi_heads.box_predictor.bbox_pred',
                             'rpn_head.conv.0': 'rpn.head.conv',
                             'rpn_head.conv.2': 'rpn.head.bbox_pred'}
            optimistic_restore(detector.detector, ckpt['state_dict'], names_map=names_map)

            detector.roi_fmap[1][0].weight.data.copy_(ckpt['state_dict']['roi_fmap.0.weight'])
            detector.roi_fmap[1][3].weight.data.copy_(ckpt['state_dict']['roi_fmap.3.weight'])
            detector.roi_fmap[1][0].bias.data.copy_(ckpt['state_dict']['roi_fmap.0.bias'])
            detector.roi_fmap[1][3].bias.data.copy_(ckpt['state_dict']['roi_fmap.3.bias'])

            detector.roi_fmap_obj[0].weight.data.copy_(ckpt['state_dict']['roi_fmap.0.weight'])
            detector.roi_fmap_obj[3].weight.data.copy_(ckpt['state_dict']['roi_fmap.3.weight'])
            detector.roi_fmap_obj[0].bias.data.copy_(ckpt['state_dict']['roi_fmap.0.bias'])
            detector.roi_fmap_obj[3].bias.data.copy_(ckpt['state_dict']['roi_fmap.3.bias'])
        elif conf.backbone == 'resnet50':
            optimistic_restore(detector, ckpt['state_dict'])
        else:
            raise NotImplementedError(conf.backbone)
        print('done')

    elif conf.mode == 'sgdet' or conf.backbone.startswith('vgg16'):
        raise ValueError('Pretrained detector should be used: use -ckpt arg to specify the path.')
    else:
        print('nothing to load form', conf.ckpt, checkpoint_path)
    return start_epoch, ckpt


def save_checkpoint(detector, optimizer, checkpoint_path, other_values=None):
    save_dir = os.path.dirname(checkpoint_path)
    if save_dir is None or len(save_dir) == 0:
        print('skip checkpointing: save_dir is not specified', save_dir)
        return
    try:
        print("\nCheckpointing to %s" % checkpoint_path)
        state_dict = {
            'state_dict': detector.state_dict(),
            'optimizer': optimizer.state_dict()
        }
        if other_values is not None:
            state_dict.update(other_values)
        torch.save(state_dict, checkpoint_path)
        print('done!\n')
    except Exception as e:
        print('error saving checkpoint', e)


def get_smallest_lr(optimizer):
    lr_min = np.Inf
    for pg in optimizer.param_groups:
        if pg['lr'] < lr_min:
            lr_min = pg['lr']
    return lr_min


def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


from torch.nn.parallel._functions import Gather
def gather_res(outputs, target_device, dim=0):
    """
    Assuming the signatures are the same accross results!
    """
    out = outputs[0]
    args = {field: Gather.apply(target_device, dim, *[getattr(o, field) for o in outputs])
            for field, v in out.__dict__.items() if v is not None}
    return type(out)(**args)


def get_ranking(predictions, labels, num_guesses=5):
    """
    Given a matrix of predictions and labels for the correct ones, get the number of guesses
    required to get the prediction right per example.
    :param predictions: [batch_size, range_size] predictions
    :param labels: [batch_size] array of labels
    :param num_guesses: Number of guesses to return
    :return:
    """
    assert labels.size(0) == predictions.size(0)
    assert labels.dim() == 1
    assert predictions.dim() == 2

    values, full_guesses = predictions.topk(predictions.size(1), dim=1)
    _, ranking = full_guesses.topk(full_guesses.size(1), dim=1, largest=False)
    gt_ranks = torch.gather(ranking.data, 1, labels.data[:, None]).squeeze()

    guesses = full_guesses[:, :num_guesses]
    return gt_ranks, guesses


def cache(f):
    """
    Caches a computation
    """
    def cache_wrapper(fn, *args, **kwargs):
        if os.path.exists(fn):
            with open(fn, 'rb') as file:
                data = pkl.load(file)
        else:
            print("file {} not found, so rebuilding".format(fn))
            data = f(*args, **kwargs)
            with open(fn, 'wb') as file:
                pkl.dump(data, file)
        return data
    return cache_wrapper


def to_variable(f):
    """
    Decorator that pushes all the outputs to a variable
    :param f: 
    :return: 
    """
    def variable_wrapper(*args, **kwargs):
        rez = f(*args, **kwargs)
        if isinstance(rez, tuple):
            return tuple([Variable(x) for x in rez])
        return Variable(rez)
    return variable_wrapper


def arange(base_tensor, n=None):
    new_size = base_tensor.size(0) if n is None else n
    new_vec = base_tensor.new(new_size).long()
    torch.arange(0, new_size, out=new_vec)
    return new_vec


def to_onehot(vec, num_classes, fill=1000):
    """
    Creates a [size, num_classes] torch FloatTensor where
    one_hot[i, vec[i]] = fill
    
    :param vec: 1d torch tensor
    :param num_classes: int
    :param fill: value that we want + and - things to be.
    :return: 
    """
    onehot_result = vec.new(vec.size(0), num_classes).float().fill_(-fill)
    arange_inds = vec.new(vec.size(0)).long()
    torch.arange(0, vec.size(0), out=arange_inds)

    onehot_result.view(-1)[vec + num_classes*arange_inds] = fill
    return onehot_result


def save_net(fname, net):
    h5f = h5py.File(fname, mode='w')
    for k, v in list(net.state_dict().items()):
        h5f.create_dataset(k, data=v.cpu().numpy())


def load_net(fname, net):
    h5f = h5py.File(fname, mode='r')
    for k, v in list(net.state_dict().items()):
        param = torch.from_numpy(np.asarray(h5f[k]))

        if v.size() != param.size():
            print("On k={} desired size is {} but supplied {}".format(k, v.size(), param.size()))
        else:
            v.copy_(param)


def batch_index_iterator(len_l, batch_size, skip_end=True):
    """
    Provides indices that iterate over a list
    :param len_l: int representing size of thing that we will
        iterate over
    :param batch_size: size of each batch
    :param skip_end: if true, don't iterate over the last batch
    :return: A generator that returns (start, end) tuples
        as it goes through all batches
    """
    iterate_until = len_l
    if skip_end:
        iterate_until = (len_l // batch_size) * batch_size

    for b_start in range(0, iterate_until, batch_size):
        yield (b_start, min(b_start+batch_size, len_l))


def batch_map(f, a, batch_size):
    """
    Maps f over the array a in chunks of batch_size.
    :param f: function to be applied. Must take in a block of
            (batch_size, dim_a) and map it to (batch_size, something).
    :param a: Array to be applied over of shape (num_rows, dim_a).
    :param batch_size: size of each array
    :return: Array of size (num_rows, something).
    """
    rez = []
    for s, e in batch_index_iterator(a.size(0), batch_size, skip_end=False):
        print("Calling on {}".format(a[s:e].size()))
        rez.append(f(a[s:e]))

    return torch.cat(rez)


def const_row(fill, l, volatile=False):
    input_tok = Variable(torch.LongTensor([fill] * l),volatile=volatile)
    if torch.cuda.is_available():
        input_tok = input_tok.cuda()
    return input_tok


def print_para(model):
    """
    Prints parameters of a model
    :param opt:
    :return:
    """
    st = {}
    strings = []
    total_params = 0
    for p_name, p in model.named_parameters():

        if not ('bias' in p_name.split('.')[-1] or 'bn' in p_name.split('.')[-1]):
            st[p_name] = ([str(x) for x in p.size()], np.prod(p.size()), p.requires_grad)
        total_params += np.prod(p.size())
    for p_name, (size, prod, p_req_grad) in sorted(st.items(), key=lambda x: -x[1][1]):
        strings.append("{:<50s}: {:<16s}({:8d}) ({})".format(
            p_name, '[{}]'.format(','.join(size)), prod, 'grad' if p_req_grad else '    '
        ))
    return '\n {:.1f}M total parameters \n ----- \n \n{}'.format(total_params / 1000000.0, '\n'.join(strings))


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def nonintersecting_2d_inds(x):
    """
    Returns np.array([(a,b) for a in range(x) for b in range(x) if a != b]) efficiently
    :param x: Size
    :return: a x*(x-1) array that is [(0,1), (0,2)... (0, x-1), (1,0), (1,2), ..., (x-1, x-2)]
    """
    rs = 1 - np.diag(np.ones(x, dtype=np.int32))
    relations = np.column_stack(np.where(rs))
    return relations


def intersect_2d(x1, x2):
    """
    Given two arrays [m1, n], [m2,n], returns a [m1, m2] array where each entry is True if those
    rows match.
    :param x1: [m1, n] numpy array
    :param x2: [m2, n] numpy array
    :return: [m1, m2] bool array of the intersections
    """
    if x1.shape[1] != x2.shape[1]:
        raise ValueError("Input arrays must have same #columns")

    # This performs a matrix multiplication-esque thing between the two arrays
    # Instead of summing, we want the equality, so we reduce in that way
    res = (x1[..., None] == x2.T[None, ...]).all(1)
    return res


def np_to_variable(x, is_cuda=True, dtype=torch.FloatTensor):
    v = Variable(torch.from_numpy(x).type(dtype))
    if is_cuda:
        v = v.cuda()
    return v


def gather_nd(x, index):
    """

    :param x: n dimensional tensor [x0, x1, x2, ... x{n-1}, dim]
    :param index: [num, n-1] where each row contains the indices we'll use
    :return: [num, dim]
    """
    nd = x.dim() - 1
    assert nd > 0
    assert index.dim() == 2
    assert index.size(1) == nd
    dim = x.size(-1)

    sel_inds = index[:,nd-1].clone()
    mult_factor = x.size(nd-1)
    for col in range(nd-2, -1, -1): # [n-2, n-3, ..., 1, 0]
        sel_inds += index[:,col] * mult_factor
        mult_factor *= x.size(col)

    grouped = x.view(-1, dim)[sel_inds]
    return grouped


def enumerate_by_image(im_inds):
    im_inds_np = im_inds.cpu().numpy()
    initial_ind = int(im_inds_np[0])
    s = 0
    for i, val in enumerate(im_inds_np):
        if val != initial_ind:
            yield initial_ind, s, i
            initial_ind = int(val)
            s = i
    yield initial_ind, s, len(im_inds_np)


def diagonal_inds(tensor):
    """
    Returns the indices required to go along first 2 dims of tensor in diag fashion
    :param tensor: thing
    :return: 
    """
    assert tensor.dim() >= 2
    assert tensor.size(0) == tensor.size(1)
    size = tensor.size(0)
    arange_inds = tensor.new(size).long()
    torch.arange(0, tensor.size(0), out=arange_inds)
    return (size+1)*arange_inds


def enumerate_imsize(im_sizes):
    s = 0
    for i, (h, w, scale, num_anchors) in enumerate(im_sizes):
        na = int(num_anchors)
        e = s + na
        yield i, s, e, h, w, scale, na

        s = e


def argsort_desc(scores):
    """
    Returns the indices that sort scores descending in a smart way
    :param scores: Numpy array of arbitrary size
    :return: an array of size [numel(scores), dim(scores)] where each row is the index you'd
             need to get the score.
    """
    return np.column_stack(np.unravel_index(np.argsort(-scores.ravel()), scores.shape))


def unravel_index(index, dims):
    unraveled = []
    index_cp = index.clone()
    for d in dims[::-1]:
        unraveled.append(index_cp % d)
        index_cp /= d
    return torch.cat([x[:,None] for x in unraveled[::-1]], 1)


def de_chunkize(tensor, chunks):
    s = 0
    for c in chunks:
        yield tensor[s:(s+c)]
        s = s+c


def random_choose(tensor, num, p=None):
    "randomly choose indices"
    num_choose = min(tensor.size(0), num)
    if num_choose == tensor.size(0):
        return tensor

    # Gotta do this in numpy because of https://github.com/pytorch/pytorch/issues/1868
    rand_idx = np.random.choice(tensor.size(0), size=num, replace=False, p=p)
    rand_idx = torch.LongTensor(rand_idx).cuda(tensor.get_device())
    chosen = tensor[rand_idx].contiguous()

    # rand_values = tensor.new(tensor.size(0)).float().normal_()
    # _, idx = torch.sort(rand_values)
    #
    # chosen = tensor[idx[:num]].contiguous()
    return chosen


def transpose_packed_sequence_inds(lengths):
    """
    Goes from a TxB packed sequence to a BxT or vice versa. Assumes that nothing is a variable
    :param ps: PackedSequence
    :return:
    """

    new_inds = []
    new_lens = []
    cum_add = np.cumsum([0] + lengths)
    max_len = lengths[0]
    length_pointer = len(lengths) - 1
    for i in range(max_len):
        while length_pointer > 0 and lengths[length_pointer] <= i:
            length_pointer -= 1
        new_inds.append(cum_add[:(length_pointer+1)].copy())
        cum_add[:(length_pointer+1)] += 1
        new_lens.append(length_pointer+1)
    new_inds = np.concatenate(new_inds, 0)
    return new_inds, new_lens


def right_shift_packed_sequence_inds(lengths):
    """
    :param lengths: e.g. [2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1]
    :return: perm indices for the old stuff (TxB) to shift it right 1 slot so as to accomodate
             BOS toks
             
             visual example: of lengths = [4,3,1,1]
    before:
    
        a (0)  b (4)  c (7) d (8)
        a (1)  b (5)
        a (2)  b (6)
        a (3)
        
    after:
    
        bos a (0)  b (4)  c (7)
        bos a (1)
        bos a (2)
        bos              
    """
    cur_ind = 0
    inds = []
    for (l1, l2) in zip(lengths[:-1], lengths[1:]):
        for i in range(l2):
            inds.append(cur_ind + i)
        cur_ind += l1
    return inds


def clip_grad_norm(named_parameters, max_norm, clip=False, verbose=False):
    r"""Clips gradient norm of an iterable of parameters.

    The norm is computed over all gradients together, as if they were
    concatenated into a single vector. Gradients are modified in-place.

    Arguments:
        parameters (Iterable[Variable]): an iterable of Variables that will have
            gradients normalized
        max_norm (float or int): max norm of the gradients

    Returns:
        Total norm of the parameters (viewed as a single vector).
    """
    max_norm = float(max_norm)

    total_norm = 0
    param_to_norm = {}
    param_to_shape = {}
    for n, p in named_parameters:
        if p.grad is not None:
            param_norm = p.grad.data.norm(2)
            total_norm += param_norm ** 2
            param_to_norm[n] = param_norm
            param_to_shape[n] = p.size()

    total_norm = total_norm ** (1. / 2)
    clip_coef = max_norm / (total_norm + 1e-6)
    if clip_coef < 1 and clip:
        for _, p in named_parameters:
            if p.grad is not None:
                p.grad.data.mul_(clip_coef)

    if verbose:
        print('---Total norm {:.3f} clip coef {:.3f}-----------------'.format(total_norm, clip_coef))
        for name, norm in sorted(param_to_norm.items(), key=lambda x: -x[1]):
            print("{:<50s}: {:.3f}, ({})".format(name, norm, param_to_shape[name]))
        print('-------------------------------', flush=True)

    return total_norm


def update_lr(optimizer, lr=1e-4):
    print("------ Learning rate -> {}".format(lr))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def set_seed(seed):
    # Set seed everywhere
    random.seed(seed)  # for some libraries
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


class Result(object):
    """ little container class for holding the detection result
        od: object detector, rm: rel model"""

    def __init__(self, od_obj_dists=None, rm_obj_dists=None,
                 obj_scores=None, obj_preds=None, obj_fmap=None,
                 od_box_deltas=None, rm_box_deltas=None,
                 od_box_targets=None, rm_box_targets=None, od_box_priors=None, rm_box_priors=None,
                 boxes_assigned=None, boxes_all=None, od_obj_labels=None, rm_obj_labels=None,
                 rpn_scores=None, rpn_box_deltas=None, rel_labels=None, rel_labels_all=None,
                 im_inds=None, fmap=None, rel_dists=None, rel_inds=None, rel_rep=None):
        self.__dict__.update(locals())
        del self.__dict__['self']

        # Remove None fields for WandB
        keys = list(self.__dict__.keys())
        for key in keys:
            if self.__dict__[key] is None:
                del self.__dict__[key]

    def is_none(self):
        return all([v is None for k, v in self.__dict__.items() if k != 'self'])

    def __getitem__(self, index):
        d = self.__dict__
        values = [d[k] for k in sorted(list(d.keys()))]
        return values[index]