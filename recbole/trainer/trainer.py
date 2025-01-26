# @Time   : 2020/6/26
# @Author : Shanlei Mu
# @Email  : slmu@ruc.edu.cn

# UPDATE:
# @Time   : 2021/6/23, 2020/9/26, 2020/9/26, 2020/10/01, 2020/9/16
# @Author : Zihan Lin, Yupeng Hou, Yushuo Chen, Shanlei Mu, Xingyu Pan
# @Email  : zhlin@ruc.edu.cn, houyupeng@ruc.edu.cn, chenyushuo@ruc.edu.cn, slmu@ruc.edu.cn, panxy@ruc.edu.cn

# UPDATE:
# @Time   : 2020/10/8, 2020/10/15, 2020/11/20, 2021/2/20, 2021/3/3, 2021/3/5, 2021/7/18
# @Author : Hui Wang, Xinyan Fan, Chen Yang, Yibo Li, Lanling Xu, Haoran Cheng, Zhichao Feng
# @Email  : hui.wang@ruc.edu.cn, xinyan.fan@ruc.edu.cn, 254170321@qq.com, 2018202152@ruc.edu.cn, xulanling_sherry@163.com, chenghaoran29@foxmail.com, fzcbupt@gmail.com

r"""
recbole.trainer.trainer
################################
"""
from distutils.command.config import config
import itertools
import os
from logging import getLogger
from time import time

import numpy as np
import torch
import torch.optim as optim
from torch.nn.utils.clip_grad import clip_grad_norm_
from tqdm import tqdm

from recbole.data.interaction import Interaction
from recbole.data.dataloader import FullSortEvalDataLoader
from recbole.evaluator import Evaluator, Collector
from recbole.utils import ensure_dir, get_local_time, early_stopping, calculate_valid_score, dict2str, \
    EvaluatorType, KGDataLoaderState, get_tensorboard, set_color, get_gpu_usage, WandbLogger


class AbstractTrainer(object):
    r"""Trainer Class is used to manage the training and evaluation processes of recommender system models.
    AbstractTrainer is an abstract class in which the fit() and evaluate() method should be implemented according
    to different training and evaluation strategies.
    """

    def __init__(self, config, model):
        self.config = config
        self.model = model

    def fit(self, train_data):
        r"""Train the model based on the train data.

        """
        raise NotImplementedError('Method [next] should be implemented.')

    def evaluate(self, eval_data):
        r"""Evaluate the model based on the eval data.

        """

        raise NotImplementedError('Method [next] should be implemented.')


class Trainer(AbstractTrainer):
    r"""The basic Trainer for basic training and evaluation strategies in recommender systems. This class defines common
    functions for training and evaluation processes of most recommender system models, including fit(), evaluate(),
    resume_checkpoint() and some other features helpful for model training and evaluation.

    Generally speaking, this class can serve most recommender system models, If the training process of the model is to
    simply optimize a single loss without involving any complex training strategies, such as adversarial learning,
    pre-training and so on.

    Initializing the Trainer needs two parameters: `config` and `model`. `config` records the parameters information
    for controlling training and evaluation, such as `learning_rate`, `epochs`, `eval_step` and so on.
    `model` is the instantiated object of a Model Class.

    """

    def __init__(self, config, model):
        super(Trainer, self).__init__(config, model)

        self.logger = getLogger()
        self.tensorboard = get_tensorboard(self.logger)
        self.wandblogger = WandbLogger(config)
        self.learner = config['learner']
        self.learning_rate = config['learning_rate']
        self.epochs = config['epochs']
        self.eval_step = min(config['eval_step'], self.epochs)
        self.stopping_step = config['stopping_step']
        self.clip_grad_norm = config['clip_grad_norm']
        self.valid_metric = config['valid_metric'].lower()
        self.valid_metric_bigger = config['valid_metric_bigger']
        self.test_batch_size = config['eval_batch_size']
        self.gpu_available = torch.cuda.is_available() and config['use_gpu']
        self.device = config['device']
        self.checkpoint_dir = config['checkpoint_dir']
        ensure_dir(self.checkpoint_dir)
        saved_model_file = '{}-{}.pth'.format(self.config['model'], get_local_time())
        self.saved_model_file = os.path.join(self.checkpoint_dir, saved_model_file)
        self.weight_decay = config['weight_decay']

        self.start_epoch = 0
        self.cur_step = 0
        self.best_valid_score = -np.inf if self.valid_metric_bigger else np.inf
        self.best_valid_result = None
        self.train_loss_dict = dict()

        self.optimizer = self._build_optimizer()

        self.eval_type = config['eval_type']
        self.eval_collector = Collector(config)
        self.evaluator = Evaluator(config)
        self.item_tensor = None
        self.tot_item_num = None

    def _build_optimizer(self, **kwargs):
        r"""Init the Optimizer

        Args:
            params (torch.nn.Parameter, optional): The parameters to be optimized.
                Defaults to ``self.model.parameters()``.
            learner (str, optional): The name of used optimizer. Defaults to ``self.learner``.
            learning_rate (float, optional): Learning rate. Defaults to ``self.learning_rate``.
            weight_decay (float, optional): The L2 regularization weight. Defaults to ``self.weight_decay``.

        Returns:
            torch.optim: the optimizer
        """
        params = kwargs.pop('params', self.model.parameters())
        learner = kwargs.pop('learner', self.learner)
        learning_rate = kwargs.pop('learning_rate', self.learning_rate)
        weight_decay = kwargs.pop('weight_decay', self.weight_decay)

        if self.config['reg_weight'] and weight_decay and weight_decay * self.config['reg_weight'] > 0:
            self.logger.warning(
                'The parameters [weight_decay] and [reg_weight] are specified simultaneously, '
                'which may lead to double regularization.'
            )

        if learner.lower() == 'adam':
            optimizer = optim.Adam(params, lr=learning_rate, weight_decay=weight_decay)
        elif learner.lower() == 'sgd':
            optimizer = optim.SGD(params, lr=learning_rate, weight_decay=weight_decay)
        elif learner.lower() == 'adagrad':
            optimizer = optim.Adagrad(params, lr=learning_rate, weight_decay=weight_decay)
        elif learner.lower() == 'rmsprop':
            optimizer = optim.RMSprop(params, lr=learning_rate, weight_decay=weight_decay)
        elif learner.lower() == 'sparse_adam':
            optimizer = optim.SparseAdam(params, lr=learning_rate)
            if weight_decay > 0:
                self.logger.warning('Sparse Adam cannot argument received argument [{weight_decay}]')
        else:
            self.logger.warning('Received unrecognized optimizer, set default Adam optimizer')
            optimizer = optim.Adam(params, lr=learning_rate)
        return optimizer

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        r"""Train the model in an epoch

        Args:
            train_data (DataLoader): The train data.
            epoch_idx (int): The current epoch id.
            loss_func (function): The loss function of :attr:`model`. If it is ``None``, the loss function will be
                :attr:`self.model.calculate_loss`. Defaults to ``None``.
            show_progress (bool): Show the progress of training epoch. Defaults to ``False``.

        Returns:
            float/tuple: The sum of loss returned by all batches in this epoch. If the loss in each batch contains
            multiple parts and the model return these multiple parts loss instead of the sum of loss, it will return a
            tuple which includes the sum of loss in each part.
        """
        self.model.train()
        loss_func = loss_func or self.model.calculate_loss
        total_loss = None
        iter_data = (
            tqdm(
                train_data,
                total=len(train_data),
                ncols=100,
                desc=set_color(f"Train {epoch_idx:>5}", 'pink'),
            ) if show_progress else train_data
        )
        for batch_idx, interaction in enumerate(iter_data):
            interaction = interaction.to(self.device)
            self.optimizer.zero_grad()
            losses = loss_func(interaction)
            if isinstance(losses, tuple):
                loss = sum(losses)
                loss_tuple = tuple(per_loss.item() for per_loss in losses)
                total_loss = loss_tuple if total_loss is None else tuple(map(sum, zip(total_loss, loss_tuple)))
            else:
                loss = losses
                total_loss = losses.item() if total_loss is None else total_loss + losses.item()
            self._check_nan(loss)
            loss.backward()
            if self.clip_grad_norm:
                clip_grad_norm_(self.model.parameters(), **self.clip_grad_norm)
            self.optimizer.step()
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))            
        
        if self.config['ips_norm']:
            with torch.no_grad():
                self.model.ips_norm()
        
        return total_loss

    def _valid_epoch(self, valid_data, show_progress=False):
        r"""Valid the model with valid data

        Args:
            valid_data (DataLoader): the valid data.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            float: valid score
            dict: valid result
        """
        valid_result = self.evaluate(valid_data, load_best_model=False, show_progress=show_progress)
        valid_score = calculate_valid_score(valid_result, self.valid_metric)
        return valid_score, valid_result

    def _save_checkpoint(self, epoch, verbose=True, **kwargs):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id

        """
        saved_model_file = kwargs.pop('saved_model_file', self.saved_model_file)
        state = {
            'config': self.config,
            'epoch': epoch,
            'cur_step': self.cur_step,
            'best_valid_score': self.best_valid_score,
            'state_dict': self.model.state_dict(),
            'other_parameter': self.model.other_parameter(),
            'optimizer': self.optimizer.state_dict(),
        }
        torch.save(state, saved_model_file)
        if verbose:
            self.logger.info(set_color('Saving current', 'blue') + f': {saved_model_file}')

    def _save_sst_embed(self, data):
        r""" save sensitive attributes and user embeddings

        Args:
            data(dataLoader): train data

        """
        checkpoint_file = self.saved_model_file
        checkpoint = torch.load(checkpoint_file)
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))
        self.model.eval()
        user_features = data.dataset.get_user_feature()
        stored_dict = self.model.get_sst_embed(user_features[1:])
        torch.save(stored_dict, self.saved_sst_embed_file)

    def resume_checkpoint(self, resume_file):
        r"""Load the model parameters information and training information.

        Args:
            resume_file (file): the checkpoint file

        """
        resume_file = str(resume_file)
        self.saved_model_file = resume_file
        checkpoint = torch.load(resume_file)
        self.start_epoch = checkpoint['epoch'] + 1
        self.cur_step = checkpoint['cur_step']
        self.best_valid_score = checkpoint['best_valid_score']

        # load architecture params from checkpoint
        if checkpoint['config']['model'].lower() != self.config['model'].lower():
            self.logger.warning(
                'Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))

        # load optimizer state from checkpoint only when optimizer type is not changed
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        message_output = 'Checkpoint loaded. Resume training from epoch {}'.format(self.start_epoch)
        self.logger.info(message_output)

    def _check_nan(self, loss):
        if torch.isnan(loss):
            raise ValueError('Training loss is nan')

    def _generate_train_loss_output(self, epoch_idx, s_time, e_time, losses):
        des = self.config['loss_decimal_place'] or 4
        train_loss_output = (set_color('epoch %d training', 'green') + ' [' + set_color('time', 'blue') +
                             ': %.2fs, ') % (epoch_idx, e_time - s_time)
        if isinstance(losses, tuple):
            des = (set_color('train_loss%d', 'blue') + ': %.' + str(des) + 'f')
            train_loss_output += ', '.join(des % (idx + 1, loss) for idx, loss in enumerate(losses))
        else:
            des = '%.' + str(des) + 'f'
            train_loss_output += set_color('train loss', 'blue') + ': ' + des % losses
        return train_loss_output + ']'

    def _add_train_loss_to_tensorboard(self, epoch_idx, losses, tag='Loss/Train'):
        if isinstance(losses, tuple):
            for idx, loss in enumerate(losses):
                self.tensorboard.add_scalar(tag + str(idx), loss, epoch_idx)
        else:
            self.tensorboard.add_scalar(tag, losses, epoch_idx)

    def _add_hparam_to_tensorboard(self, best_valid_result):
        # base hparam
        hparam_dict = {
            'learner': self.config['learner'],
            'learning_rate': self.config['learning_rate'],
            'train_batch_size': self.config['train_batch_size']
        }
        # unrecorded parameter
        unrecorded_parameter = {
            parameter
            for parameters in self.config.parameters.values() for parameter in parameters
        }.union({'model', 'dataset', 'config_files', 'device'})
        # other model-specific hparam
        hparam_dict.update({
            para: val
            for para, val in self.config.final_config_dict.items() if para not in unrecorded_parameter
        })
        for k in hparam_dict:
            if hparam_dict[k] is not None and not isinstance(hparam_dict[k], (bool, str, float, int)):
                hparam_dict[k] = str(hparam_dict[k])

        self.tensorboard.add_hparams(hparam_dict, {'hparam/best_valid_result': best_valid_result})

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        r"""Train the model based on the train data and the valid data.

        Args:
            train_data (DataLoader): the train data
            valid_data (DataLoader, optional): the valid data, default: None.
                                               If it's None, the early_stopping is invalid.
            verbose (bool, optional): whether to write training and evaluation information to logger, default: True
            saved (bool, optional): whether to save the model parameters, default: True
            show_progress (bool): Show the progress of training epoch and evaluate epoch. Defaults to ``False``.
            callback_fn (callable): Optional callback function executed at end of epoch.
                                    Includes (epoch_idx, valid_score) input arguments.

        Returns:
             (float, dict): best valid score and best valid result. If valid_data is None, it returns (-1, None)
        """
        if saved and self.start_epoch >= self.epochs:
            self._save_checkpoint(-1, verbose=verbose)

        self.eval_collector.data_collect(train_data)
        if self.config['train_neg_sample_args'].get('dynamic', 'none') != 'none':
            train_data.get_model(self.model)
        valid_step = 0

        for epoch_idx in range(self.start_epoch, self.epochs):
            # train
            training_start_time = time()
            train_loss = self._train_epoch(train_data, epoch_idx, show_progress=show_progress)
            self.train_loss_dict[epoch_idx] = sum(train_loss) if isinstance(train_loss, tuple) else train_loss
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            self._add_train_loss_to_tensorboard(epoch_idx, train_loss)
            self.wandblogger.log_metrics({'epoch': epoch_idx, 'train_loss': train_loss, 'train_step': epoch_idx},
                                         head='train')

            # eval
            if self.eval_step <= 0 or not valid_data:
                if saved:
                    self._save_checkpoint(epoch_idx, verbose=verbose)
                continue
            if (epoch_idx + 1) % self.eval_step == 0:
                valid_start_time = time()
                valid_score, valid_result = self._valid_epoch(valid_data, show_progress=show_progress)
                self.best_valid_score, self.cur_step, stop_flag, update_flag = early_stopping(
                    valid_score,
                    self.best_valid_score,
                    self.cur_step,
                    max_step=self.stopping_step,
                    bigger=self.valid_metric_bigger
                )
                valid_end_time = time()
                valid_score_output = (set_color("epoch %d evaluating", 'green') + " [" + set_color("time", 'blue')
                                      + ": %.2fs, " + set_color("valid_score", 'blue') + ": %f]") % \
                                     (epoch_idx, valid_end_time - valid_start_time, valid_score)
                valid_result_output = set_color('valid result', 'blue') + ': \n' + dict2str(valid_result)
                if verbose:
                    self.logger.info(valid_score_output)
                    self.logger.info(valid_result_output)
                self.tensorboard.add_scalar('Vaild_score', valid_score, epoch_idx)
                self.wandblogger.log_metrics({**valid_result, 'valid_step': valid_step}, head='valid')

                if update_flag:
                    if saved:
                        self._save_checkpoint(epoch_idx, verbose=verbose)
                    self.best_valid_result = valid_result

                if callback_fn:
                    callback_fn(epoch_idx, valid_score)

                if stop_flag:
                    stop_output = 'Finished training, best eval result in epoch %d' % \
                                  (epoch_idx - self.cur_step * self.eval_step)
                    if verbose:
                        self.logger.info(stop_output)
                    break

                valid_step += 1

        # store embedding and sst if task need attacker after training
        if self.config['save_sst_embed']:
            self._save_sst_embed(train_data)

        self._add_hparam_to_tensorboard(self.best_valid_score)
        return self.best_valid_score, self.best_valid_result

    def _full_sort_batch_eval(self, batched_data):
        interaction, history_index, positive_u, positive_i = batched_data
        try:
            # Note: interaction without item ids
            scores = self.model.full_sort_predict(interaction.to(self.device))
        except NotImplementedError:
            inter_len = len(interaction)
            new_inter = interaction.to(self.device).repeat_interleave(self.tot_item_num)
            batch_size = len(new_inter)
            new_inter.update(self.item_tensor.repeat(inter_len))
            if batch_size <= self.test_batch_size:
                scores = self.model.predict(new_inter)
            else:
                scores = self._spilt_predict(new_inter, batch_size)

        scores = scores.view(-1, self.tot_item_num)
        scores[:, 0] = -np.inf
        if history_index is not None:
            scores[history_index] = -np.inf
        return interaction, scores, positive_u, positive_i

    def _neg_sample_batch_eval(self, batched_data):
        interaction, row_idx, positive_u, positive_i = batched_data
        batch_size = interaction.length
        if batch_size <= self.test_batch_size:
            origin_scores = self.model.predict(interaction.to(self.device))
        else:
            origin_scores = self._spilt_predict(interaction, batch_size)

        if self.config['eval_type'] == EvaluatorType.VALUE:
            return interaction, origin_scores, positive_u, positive_i
        elif self.config['eval_type'] == EvaluatorType.RANKING:
            col_idx = interaction[self.config['ITEM_ID_FIELD']]
            batch_user_num = positive_u[-1] + 1
            scores = torch.full((batch_user_num, self.tot_item_num), -np.inf, device=self.device)
            scores[row_idx.long(), col_idx.long()] = origin_scores.view(-1)
            return interaction, scores, positive_u, positive_i

    @torch.no_grad()
    def evaluate(self, eval_data, load_best_model=False, model_file=None, show_progress=False):
        r"""Evaluate the model based on the eval data.

        Args:
            eval_data (DataLoader): the eval data
            load_best_model (bool, optional): whether load the best model in the training process, default: True.
                                              It should be set True, if users want to test the model after training.
            model_file (str, optional): the saved model file, default: None. If users want to test the previously
                                        trained model file, they can set this parameter.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            collections.OrderedDict: eval result, key is the eval metric and value in the corresponding metric value.
        """
        if not eval_data:
            return

        if load_best_model:
            checkpoint_file = model_file or self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if isinstance(eval_data, FullSortEvalDataLoader):
            eval_func = self._full_sort_batch_eval
            if self.item_tensor is None:
                self.item_tensor = eval_data.dataset.get_item_feature().to(self.device)
        else:
            eval_func = self._neg_sample_batch_eval
        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )

        self.eval_collector.model_collect(self.model)
        for batch_idx, batched_data in enumerate(iter_data):
            torch.cuda.empty_cache() 
            interaction, scores, positive_u, positive_i = eval_func(batched_data)
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
            self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)
        struct = self.eval_collector.get_data_struct()
        result = self.evaluator.evaluate(struct)
        self.wandblogger.log_eval_metrics(result, head='eval')

        return result

    def _spilt_predict(self, interaction, batch_size):
        spilt_interaction = dict()
        for key, tensor in interaction.interaction.items():
            spilt_interaction[key] = tensor.split(self.test_batch_size, dim=0)
        num_block = (batch_size + self.test_batch_size - 1) // self.test_batch_size
        result_list = []
        for i in range(num_block):
            current_interaction = dict()
            for key, spilt_tensor in spilt_interaction.items():
                current_interaction[key] = spilt_tensor[i]
            result = self.model.predict(Interaction(current_interaction).to(self.device))
            if len(result.shape) == 0:
                result = result.unsqueeze(0)
            result_list.append(result)
        return torch.cat(result_list, dim=0)


class FairGoTrainer(Trainer):
    def __init__(self, config, model):
        super(FairGoTrainer, self).__init__(config, model)

        self.train_epoch_interval = config['train_epoch_interval']
        self.sst_num = len(self.config['sst_attr_list'])
        self.mask_label = {i:sst for i, sst in enumerate(self.config['sst_attr_list'])}
        self.load_pretrain_weight = config['load_pretrain_weight']
        if config['pretrain_model_file_path'] is not None:
            self.saved_pretrain_model_file = config['pretrain_model_file_path'] 
            checkpoint_file = config['pretrain_model_file_path']
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading pretrain model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)
            self.model.train_stage = 'finetune'
        elif self.load_pretrain_weight:
            self.model.train_stage = 'finetune'
        else:
            self.model.train_stage = 'pretrain'
            self.pretrain_epochs = config['pretrain_epochs']

        saved_sst_embed_file = '{}-{}_embed-[{}].pth'.format(self.config['model'], self.config['aggr_method'], '_'.join(self.config['sst_attr_list']))
        # saved_sst_embed_file = '{}_embed-[{}]-{}.pth'.format(self.config['model'], '_'.join(self.config['sst_attr_list']), self.config['filter_mode'])
        self.saved_sst_embed_file = os.path.join(self.checkpoint_dir, saved_sst_embed_file)

    def reset_params(self):
        config = self.config
        self.learner = config['learner']
        self.learning_rate = config['learning_rate']
        self.epochs = config['epochs']
        self.eval_step = min(config['eval_step'], self.epochs)

        self.start_epoch = 0
        self.cur_step = 0
        self.best_valid_score = -np.inf if self.valid_metric_bigger else np.inf
        self.best_valid_result = None
        self.train_loss_dict = dict()
        self.eval_type = config['eval_type']
        self.eval_collector = Collector(config)
        self.evaluator = Evaluator(config)
        self.item_tensor = None
        self.tot_item_num = None

        self.model.train_stage = 'finetune'

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        if self.model.train_stage == 'pretrain':
            self.pretrain(train_data, valid_data, verbose, saved, show_progress)
            self.reset_params()
            return super().fit(train_data, valid_data, verbose, saved, show_progress, callback_fn)
        elif self.model.train_stage == 'finetune':
            return super().fit(train_data, valid_data, verbose, saved, show_progress, callback_fn)
        else:
            raise ValueError("Please make sure that the 'train_stage' is 'pretrain' or 'finetune'!")

    def save_pretrained_model(self, saved_model_file):
        r"""Store the model parameters information and training information.

        Args:
            saved_model_file (str): file name for saved pretrained model

        """
        state = {
            'config': self.config,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'other_parameter': self.model.other_parameter(),
        }
        torch.save(state, saved_model_file)

    def pretrain(self, train_data, valid_data, verbose=True, saved=True, show_progress=False):
        self.saved_pretrain_model_file = os.path.join(
            self.checkpoint_dir,
            '{}-{}-pretrain.pth'.format(self.config['model'], self.config['dataset'])
        )

        self.saved_pretrain_sst_file = os.path.join(
            self.checkpoint_dir,
            '{}-{}-pretrain_embed[none].pth'.format(self.config['model'], self.config['dataset'])
        )
        self.eval_step = min(self.config['eval_step'], self.pretrain_epochs)
        self.logger.info(set_color('Model Pretrain', 'yellow'))
        self.optimizer = self.optimizer_pretrain
        valid_step = 0 
        self.eval_collector.data_collect(train_data)

        for epoch_idx in range(self.start_epoch, self.pretrain_epochs):
            # train
            training_start_time = time()
            train_loss = self._train_epoch_with_mask(train_data, epoch_idx, show_progress=show_progress, sst_list=None)
            self.train_loss_dict[epoch_idx] = sum(train_loss) if isinstance(train_loss, tuple) else train_loss
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            self._add_train_loss_to_tensorboard(epoch_idx, train_loss)

            # eval
            if self.eval_step <= 0 or not valid_data:
                if saved:
                    self.save_pretrained_model(self.saved_pretrain_model_file)
                continue
            if (epoch_idx + 1) % self.eval_step == 0:
                valid_start_time = time()
                valid_score, valid_result = self._valid_epoch(valid_data, show_progress=show_progress)
                self.best_valid_score, self.cur_step, stop_flag, update_flag = early_stopping(
                    valid_score,
                    self.best_valid_score,
                    self.cur_step,
                    max_step=self.stopping_step,
                    bigger=self.valid_metric_bigger
                )
                valid_end_time = time()
                valid_score_output = (set_color("epoch %d evaluating", 'green') + " [" + set_color("time", 'blue')
                                      + ": %.2fs, " + set_color("valid_score", 'blue') + ": %f]") % \
                                     (epoch_idx, valid_end_time - valid_start_time, valid_score)
                valid_result_output = set_color('valid result', 'blue') + ': \n' + dict2str(valid_result)
                if verbose:
                    self.logger.info(valid_score_output)
                    self.logger.info(valid_result_output)
                self.tensorboard.add_scalar('Vaild_score', valid_score, epoch_idx)
                self.wandblogger.log_metrics({**valid_result, 'valid_step': valid_step}, head='valid')

                if update_flag:
                    if saved:
                        update_output = set_color('Saving current best', 'blue') + ': %s' % self.saved_pretrain_model_file
                        if verbose:
                            self.logger.info(update_output)
                        self.save_pretrained_model(self.saved_pretrain_model_file)
                    self.best_valid_result = valid_result

                if stop_flag:
                    stop_output = 'Finished pretraining, best eval result in epoch %d' % \
                                  (epoch_idx - self.cur_step * self.eval_step)
                    if verbose:
                        self.logger.info(stop_output)
                    break

                valid_step += 1

        checkpoint = torch.load(self.saved_pretrain_model_file)
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))
        # store embedding and sst if task need attacker after training
        if self.config['save_sst_embed']:
            self._save_sst_embed(train_data, self.saved_pretrain_sst_file)

        self._add_hparam_to_tensorboard(self.best_valid_score)
        return self.best_valid_score, self.best_valid_result

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        dis_loss, filter_loss = 0., 0.
        mask = np.zeros(self.sst_num)
        while mask.sum() == 0:
            mask = np.random.choice([0,1], self.sst_num)
        sst_list = [sst for i, sst in self.mask_label.items() if mask[i]!=0]        
        if epoch_idx % self.train_epoch_interval == 0:
            self.optimizer = self.optimizer_filter
            self.logger.info('Train Filter')
            filter_loss = self._train_epoch_with_mask(train_data, epoch_idx, self.model.calculate_loss, sst_list,
                                               show_progress)
        
        self.optimizer = self.optimizer_dis
        self.logger.info('Train Discriminator')
        dis_loss = self._train_epoch_with_mask(train_data, epoch_idx, self.model.calculate_dis_loss, sst_list,
                                        show_progress)

        return dis_loss, filter_loss
    
    def _train_epoch_with_mask(self, train_data, epoch_idx, loss_func=None, sst_list=None, show_progress=False):
        self.model.train()
        loss_func = loss_func or self.model.calculate_loss
        total_loss = None
        iter_data = (
            tqdm(
                train_data,
                total=len(train_data),
                ncols=100,
                desc=set_color(f"Train {epoch_idx:>5}", 'pink'),
            ) if show_progress else train_data
        )
        for batch_idx, interaction in enumerate(iter_data):
            interaction = interaction.to(self.device)
            self.optimizer.zero_grad()
            losses = loss_func(interaction, sst_list)
            if isinstance(losses, tuple):
                loss = sum(losses)
                loss_tuple = tuple(per_loss.item() for per_loss in losses)
                total_loss = loss_tuple if total_loss is None else tuple(map(sum, zip(total_loss, loss_tuple)))
            else:
                loss = losses
                total_loss = losses.item() if total_loss is None else total_loss + losses.item()
            self._check_nan(loss)
            loss.backward()
            if self.clip_grad_norm:
                clip_grad_norm_(self.model.parameters(), **self.clip_grad_norm)
            self.optimizer.step()
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
        return total_loss

    @torch.no_grad()
    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if not eval_data:
            return

        result = {}
        if not load_best_model:
            result = super().evaluate(eval_data, show_progress=show_progress)
            return result

        if load_best_model and not self.load_pretrain_weight:
            checkpoint_file = self.saved_pretrain_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            self.model.train_stage = 'pretrain'
            message_output = 'Loading pretrain model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)
            result_tmp = super().evaluate(eval_data)
            for key, value in result_tmp.items():
                result[f'pretrain-{key}'] = value

        if load_best_model:
            checkpoint_file = model_file or self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            self.model.train_stage = 'finetune'
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)
            result_tmp = super().evaluate(eval_data)
            for key, value in result_tmp.items():
                result[f'finetune-{key}'] = value

        return result

    def _save_sst_embed(self, data, saved_sst_embed_file=None):
        self.model.eval()
        user_features = data.dataset.get_user_feature()[1:]

        attr_list = [_ for _ in self.mask_label.values()]
        stored_dict = self.model.get_sst_embed(user_features, attr_list)
        if saved_sst_embed_file is None:
            saved_sst_embed_file = self.saved_sst_embed_file
        torch.save(stored_dict, saved_sst_embed_file)

    def _save_checkpoint(self, epoch, verbose=True, **kwargs):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id

        """
        saved_model_file = kwargs.pop('saved_model_file', self.saved_model_file)
        state = {
            'config': self.config,
            'epoch': epoch,
            'cur_step': self.cur_step,
            'best_valid_score': self.best_valid_score,
            'state_dict': self.model.state_dict(),
            'other_parameter': self.model.other_parameter(),
            'optimizer': self.optimizer.state_dict(),
            'optimizer_filter': self.optimizer_filter.state_dict(),
            'optimizer_dis': self.optimizer_dis.state_dict()
        }
        torch.save(state, saved_model_file)
        if verbose:
            self.logger.info(set_color('Saving current', 'blue') + f': {saved_model_file}')

    def resume_checkpoint(self, resume_file):
        r"""Load the model parameters information and training information.

        Args:
            resume_file (file): the checkpoint file

        """
        resume_file = str(resume_file)
        self.saved_model_file = resume_file
        checkpoint = torch.load(resume_file)
        self.start_epoch = checkpoint['epoch'] + 1
        self.cur_step = checkpoint['cur_step']
        self.best_valid_score = checkpoint['best_valid_score']

        # load architecture params from checkpoint
        if checkpoint['config']['model'].lower() != self.config['model'].lower():
            self.logger.warning(
                'Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))

        # load optimizer state from checkpoint only when optimizer type is not changed
        self.optimizer_filter.load_state_dict(checkpoint['optimizer_filter'])
        self.optimizer_dis.load_state_dict(checkpoint['optimizer_dis'])
        message_output = 'Checkpoint loaded. Resume training from epoch {}'.format(self.start_epoch)
        self.logger.info(message_output)


class FairGo_PMFTrainer(FairGoTrainer):
    def __init__(self, config, model):
        super(FairGo_PMFTrainer, self).__init__(config, model)
        if not self.load_pretrain_weight:
            self.optimizer_pretrain = self._build_optimizer(params=[model.user_embedding_layer.weight]+[model.item_embedding_layer.weight])
        if config['aggr_method'] == 'LBA':
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()]
                                                    + [{'params':list(self.model.aggr_layer.parameters())}])
        else:
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])
        self.optimizer_filter = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.filter_layer_dict.values()])


class FairGo_GCNTrainer(FairGoTrainer):
    def __init__(self, config, model):
        super(FairGo_GCNTrainer, self).__init__(config, model)
        if not self.load_pretrain_weight:
            self.optimizer_pretrain = self._build_optimizer(params=[{'params':model.user_embedding_layer.weight}]
                                                        +[{'params':model.item_embedding_layer.weight}]
                                                        +[{'params':model.gcn.parameters()}])
        if config['aggr_method'] == 'LBA':
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()]
                                                    + [{'params':list(self.model.aggr_layer.parameters())}])
        else:
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])
        self.optimizer_filter = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.filter_layer_dict.values()])


class PFCNTrainer(Trainer):
    def __init__(self, config, model):
        super(PFCNTrainer, self).__init__(config, model)

        self.filter_mode = config['filter_mode'].lower()
        self.train_epoch_interval = config['train_epoch_interval']
        if self.filter_mode != 'none':
            self.sst_num = len(self.config['sst_attr_list'])
            self.mask_label = {i:sst for i, sst in enumerate(self.config['sst_attr_list'])}

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        dis_loss, filter_loss = 0., 0.

        if self.filter_mode != 'none':
            mask = np.zeros(self.sst_num)
            while mask.sum() == 0:
                mask = np.random.choice([0,1], self.sst_num)
            sst_list = [sst for i, sst in self.mask_label.items() if mask[i]!=0]
            if epoch_idx % self.config['train_epoch_interval'] == 0:
                self.logger.info('Train Filter and Base model')
                self.optimizer = self.optimizer_filter
                filter_loss = self._train_epoch_with_mask(train_data, epoch_idx, self.model.calculate_loss, sst_list,
                                                show_progress)

            self.logger.info('Train Discriminator')
            self.optimizer = self.optimizer_dis
            dis_loss = self._train_epoch_with_mask(train_data, epoch_idx, self.model.calculate_dis_loss, sst_list,
                                            show_progress)

            return filter_loss, dis_loss
        else:
            filter_loss = self._train_epoch_with_mask(train_data, epoch_idx, self.model.calculate_loss, None, show_progress)
            
            return filter_loss

    def _train_epoch_with_mask(self, train_data, epoch_idx, loss_func=None, sst_list=None, show_progress=False):
        self.model.train()
        loss_func = loss_func or self.model.calculate_loss
        total_loss = None
        iter_data = (
            tqdm(
                train_data,
                total=len(train_data),
                ncols=100,
                desc=set_color(f"Train {epoch_idx:>5}", 'pink'),
            ) if show_progress else train_data
        )
        for batch_idx, interaction in enumerate(iter_data):
            interaction = interaction.to(self.device)
            self.optimizer.zero_grad()
            losses = loss_func(interaction, sst_list)
            if isinstance(losses, tuple):
                loss = sum(losses)
                loss_tuple = tuple(per_loss.item() for per_loss in losses)
                total_loss = loss_tuple if total_loss is None else tuple(map(sum, zip(total_loss, loss_tuple)))
            else:
                loss = losses
                total_loss = losses.item() if total_loss is None else total_loss + losses.item()
            self._check_nan(loss)
            loss.backward()
            if self.clip_grad_norm:
                clip_grad_norm_(self.model.parameters(), **self.clip_grad_norm)
            self.optimizer.step()
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
        return total_loss

    def _neg_sample_batch_eval(self, batched_data, sst_list=None):
        interaction, row_idx, positive_u, positive_i = batched_data
        batch_size = interaction.length
        if batch_size <= self.test_batch_size:
            origin_scores = self.model.predict(interaction.to(self.device), sst_list)
        else:
            origin_scores = self._spilt_predict(interaction, batch_size, sst_list)

        if self.config['eval_type'] == EvaluatorType.VALUE:
            return interaction, origin_scores, positive_u, positive_i
        elif self.config['eval_type'] == EvaluatorType.RANKING:
            col_idx = interaction[self.config['ITEM_ID_FIELD']]
            batch_user_num = positive_u[-1] + 1
            scores = torch.full((batch_user_num, self.tot_item_num), -np.inf, device=self.device)
            scores[row_idx.long(), col_idx.long()] = origin_scores.view(-1)
            return interaction, scores, positive_u, positive_i

    def _spilt_predict(self, interaction, batch_size, sst_list):
        spilt_interaction = dict()
        for key, tensor in interaction.interaction.items():
            spilt_interaction[key] = tensor.split(self.test_batch_size, dim=0)
        num_block = (batch_size + self.test_batch_size - 1) // self.test_batch_size
        result_list = []
        for i in range(num_block):
            current_interaction = dict()
            for key, spilt_tensor in spilt_interaction.items():
                current_interaction[key] = spilt_tensor[i]
            result = self.model.predict(Interaction(current_interaction).to(self.device), sst_list)
            if len(result.shape) == 0:
                result = result.unsqueeze(0)
            result_list.append(result)
        return torch.cat(result_list, dim=0)

    @torch.no_grad()
    def pfcn_evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        r"""Evaluate the model based on the eval data.

        Args:
            eval_data (DataLoader): the eval data
            load_best_model (bool, optional): whether load the best model in the training process, default: True.
                                              It should be set True, if users want to test the model after training.
            model_file (str, optional): the saved model file, default: None. If users want to test the previously
                                        trained model file, they can set this parameter.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            collections.OrderedDict: eval result, key is the eval metric and value in the corresponding metric value.
        """
        if not eval_data:
            return

        if load_best_model:
            checkpoint_file = model_file or self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if isinstance(eval_data, FullSortEvalDataLoader):
            eval_func = self._full_sort_batch_eval
            if self.item_tensor is None:
                self.item_tensor = eval_data.dataset.get_item_feature().to(self.device)
        else:
            eval_func = self._neg_sample_batch_eval
        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )
        for batch_idx, batched_data in enumerate(iter_data):
            if self.filter_mode != 'none':
                for i in range(1, self.sst_num+1):
                    sst_lists = [list(_) for _ in itertools.combinations(self.config['sst_attr_list'], i)]
                    for sst_list in sst_lists:
                        interaction, scores, positive_u, positive_i = eval_func(batched_data, sst_list)
                        if self.gpu_available and show_progress:
                            iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
                        self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)
            else:
                interaction, scores, positive_u, positive_i = eval_func(batched_data)
                if self.gpu_available and show_progress:
                    iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
                self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)

        self.eval_collector.model_collect(self.model)
        struct = self.eval_collector.get_data_struct()
        result = self.evaluator.evaluate(struct)
        self.wandblogger.log_eval_metrics(result, head='eval')

        return result

    def _valid_epoch(self, valid_data, show_progress=False):
        r"""Valid the model with valid data

        Args:
            valid_data (DataLoader): the valid data.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            float: valid score
            dict: valid result
        """
        valid_result = self.pfcn_evaluate(valid_data, load_best_model=False, show_progress=show_progress)
        valid_score = calculate_valid_score(valid_result, self.valid_metric)
        return valid_score, valid_result

    @torch.no_grad()
    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if not eval_data:
            return

        if load_best_model:
            checkpoint_file = model_file or self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if isinstance(eval_data, FullSortEvalDataLoader):
            eval_func = self._full_sort_batch_eval
            if self.item_tensor is None:
                self.item_tensor = eval_data.dataset.get_item_feature().to(self.device)
        else:
            eval_func = self._neg_sample_batch_eval
        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )
        final_result = {}
        if self.filter_mode != 'none':
            for i in range(1, self.sst_num+1):
                sst_lists = [list(_) for _ in itertools.combinations(self.config['sst_attr_list'], i)]
                for sst_list in sst_lists:
                    for batch_idx, batched_data in enumerate(iter_data):
                        interaction, scores, positive_u, positive_i = eval_func(batched_data, sst_list)
                        if self.gpu_available and show_progress:
                            iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
                        self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)
                    self.eval_collector.model_collect(self.model)
                    struct = self.eval_collector.get_data_struct()
                    result = self.evaluator.evaluate(struct)
                    final_result['{}-{}'.format(self.config['filter_mode'], sst_list)] = result
                    self.wandblogger.log_eval_metrics(result, head='eval')
        else:
            for batch_idx, batched_data in enumerate(iter_data):
                interaction, scores, positive_u, positive_i = eval_func(batched_data)
                if self.gpu_available and show_progress:
                    iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
                self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)
            self.eval_collector.model_collect(self.model)
            struct = self.eval_collector.get_data_struct()
            result = self.evaluator.evaluate(struct)
            final_result[self.config['filter_mode']] = result
            self.wandblogger.log_eval_metrics(result, head='eval')

        return final_result

    def _save_sst_embed(self, data):
        checkpoint_file = self.saved_model_file
        checkpoint = torch.load(checkpoint_file)
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))
        self.model.eval()
        user_features = data.dataset.get_user_feature()[1:]

        if self.filter_mode != 'none':
            for i in range(1,4):
                attr_lists = [list(_) for _ in itertools.combinations(self.config['sst_attr_list'],i)]
                for attr_list in attr_lists:
                    stored_dict = self.model.get_sst_embed(user_features, attr_list)
                    saved_sst_embed_file = '{}_embed-{}-[{}].pth'.format(self.config['model'],
                                                                         self.config['filter_mode'],
                                                                         '_'.join(attr_list),)
                    saved_sst_embed_file = os.path.join(self.checkpoint_dir, saved_sst_embed_file)
                    torch.save(stored_dict, saved_sst_embed_file)
        else:
            stored_dict = self.model.get_sst_embed(user_features)
            saved_sst_embed_file = '{}_embed-{}.pth'.format(self.config['model'],
                                                                 self.config['filter_mode'])
            saved_sst_embed_file = os.path.join(self.checkpoint_dir, saved_sst_embed_file)
            torch.save(stored_dict, saved_sst_embed_file)

    def _save_checkpoint(self, epoch, verbose=True, **kwargs):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id

        """
        saved_model_file = kwargs.pop('saved_model_file', self.saved_model_file)
        state = {
            'config': self.config,
            'epoch': epoch,
            'cur_step': self.cur_step,
            'best_valid_score': self.best_valid_score,
            'state_dict': self.model.state_dict(),
            'other_parameter': self.model.other_parameter(),
            'optimizer': self.optimizer.state_dict(),
            'optimizer_filter': self.optimizer_filter.state_dict() if self.filter_mode !='none' else None,
            'optimizer_dis': self.optimizer_dis.state_dict() if self.filter_mode !='none' else None
        }
        torch.save(state, saved_model_file)
        if verbose:
            self.logger.info(set_color('Saving current', 'blue') + f': {saved_model_file}')

    def resume_checkpoint(self, resume_file):
        r"""Load the model parameters information and training information.

        Args:
            resume_file (file): the checkpoint file

        """
        resume_file = str(resume_file)
        self.saved_model_file = resume_file
        checkpoint = torch.load(resume_file)
        self.start_epoch = checkpoint['epoch'] + 1
        self.cur_step = checkpoint['cur_step']
        self.best_valid_score = checkpoint['best_valid_score']

        # load architecture params from checkpoint
        if checkpoint['config']['model'].lower() != self.config['model'].lower():
            self.logger.warning(
                'Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))

        # load optimizer state from checkpoint only when optimizer type is not changed
        if self.filter_mode != 'none':
            self.optimizer.load_state_dict(checkpoint['optimizer'])
        else:
            self.optimizer_filter.load_state_dict(checkpoint['optimizer_filter'])
            self.optimizer_dis.load_state_dict(checkpoint['optimizer_dis'])
        message_output = 'Checkpoint loaded. Resume training from epoch {}'.format(self.start_epoch)
        self.logger.info(message_output)


class PFCN_MLPTrainer(PFCNTrainer):
    def __init__(self, config, model):
        super(PFCN_MLPTrainer, self).__init__(config, model)

        if self.filter_mode != 'none':
            self.optimizer_filter = self._build_optimizer(params=[{'params':self.model.user_embedding.weight}]
                                                        + [{'params':self.model.item_embedding.weight}]
                                                        + [{'params':_.parameters()} for _ in model.filter_layer.values()]
                                                        + [{'params':model.mlp_layer.parameters()}])
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])


class PFCN_BiasedMFTrainer(PFCNTrainer):
    def __init__(self, config, model):
        super(PFCN_BiasedMFTrainer, self).__init__(config, model)

        if self.filter_mode != 'none':
            self.optimizer_filter = self._build_optimizer(params=[{'params':self.model.user_embedding_layer.weight}]
                                                        + [{'params':self.model.item_embedding_layer.weight}]
                                                        + [{'params':_.parameters()} for _ in model.filter_layer.values()]
                                                        + [{'params':self.model.user_bias.weight}]
                                                        + [{'params':self.model.item_bias.weight}]
                                                        + [{'params':self.model.global_bias}])
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])


class PFCN_DMFTrainer(PFCNTrainer):
    def __init__(self, config, model):
        super(PFCN_DMFTrainer, self).__init__(config, model)

        if self.filter_mode != 'none':
            self.optimizer_filter = self._build_optimizer(params=[{'params':self.model.user_embedding_layer.weight}]
                                                        + [{'params':self.model.item_embedding_layer.weight}]
                                                        + [{'params':_.parameters()} for _ in model.filter_layer.values()]
                                                        + [{'params':self.model.user_mlp.parameters()}]
                                                        + [{'params':self.model.item_mlp.parameters()}])
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])


class PFCN_PMFTrainer(PFCNTrainer):
    def __init__(self, config, model):
        super(PFCN_PMFTrainer, self).__init__(config, model)

        if self.filter_mode != 'none':
            self.optimizer_filter = self._build_optimizer(params=[{'params':self.model.user_embedding_layer.weight}]
                                                        + [{'params':self.model.item_embedding_layer.weight}]
                                                        + [{'params':_.parameters()} for _ in model.filter_layer.values()])
            self.optimizer_dis = self._build_optimizer(params=[{'params':_.parameters()} for _ in model.dis_layer_dict.values()])