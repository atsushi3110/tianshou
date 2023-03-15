from typing import Any, Dict, List, Optional, Type

import numpy as np
import torch
from torch import nn

from tianshou.data import Batch, ReplayBuffer, to_torch_as, to_torch
from tianshou.policy import A2CPolicy
from tianshou.utils.net.common import ActorCritic
from  torch.distributions import Categorical



class AdaptiveKLController:
    """Adaptive KL Controller as described in Ziegler et al. "Fine-Tuning Language Models from Human Preferences"
    Reference: Section 2.2 https://arxiv.org/pdf/1909.08593.pdf#page=2
    Source: https://github.com/openai/lm-human-preferences/blob/master/lm_human_preferences/train_policy.py
    """

    def __init__(self, init_kl_coef: float, target: float, horizon: int, clip:float=0.2, is_fixed:bool=False):
        self.value = init_kl_coef
        self.target = target
        self.horizon = horizon
        self.c = clip
        self.is_fixed = is_fixed

    def update(self, current: float, n_steps: int):
        """Returns adaptively updated KL coefficient, βₜ₊₁.
        Arguments:
            current: The current KL value between the newest policy and the initial policy.
        """
        if self.is_fixed:
            return 

        with torch.no_grad():
            proportional_error = torch.clip(current / self.target - 1, -self.c, self.c)  # ϵₜ
            mult = 1 + proportional_error * n_steps / self.horizon
            self.value *= mult  # βₜ₊₁


class LangPPOPolicy(A2CPolicy):
    r"""Implementation of Proximal Policy Optimization. arXiv:1707.06347. for Language Models

    :param torch.nn.Module actor: the actor network following the rules in
        :class:`~tianshou.policy.BasePolicy`. (s -> logits)
    :param torch.nn.Module critic: the critic network. (s -> V(s))
    :param torch.optim.Optimizer optim: the optimizer for actor and critic network.
    :param dist_fn: distribution class for computing the action.
    :type dist_fn: Type[torch.distributions.Distribution]
    :param float discount_factor: in [0, 1]. Default to 0.99.
    :param float eps_clip: :math:`\epsilon` in :math:`L_{CLIP}` in the original
        paper. Default to 0.2.
    :param float dual_clip: a parameter c mentioned in arXiv:1912.09729 Equ. 5,
        where c > 1 is a constant indicating the lower bound.
        Default to 5.0 (set None if you do not want to use it).
    :param bool value_clip: a parameter mentioned in arXiv:1811.02553v3 Sec. 4.1.
        Default to True.
    :param bool advantage_normalization: whether to do per mini-batch advantage
        normalization. Default to True.
    :param bool recompute_advantage: whether to recompute advantage every update
        repeat according to https://arxiv.org/pdf/2006.05990.pdf Sec. 3.5.
        Default to False.
    :param float vf_coef: weight for value loss. Default to 0.5.
    :param float ent_coef: weight for entropy loss. Default to 0.01.
    :param float max_grad_norm: clipping gradients in back propagation. Default to
        None.
    :param float gae_lambda: in [0, 1], param for Generalized Advantage Estimation.
        Default to 0.95.
    :param bool reward_normalization: normalize estimated values to have std close
        to 1, also normalize the advantage to Normal(0, 1). Default to False.
    :param int max_batchsize: the maximum size of the batch when computing GAE,
        depends on the size of available memory and the memory cost of the model;
        should be as large as possible within the memory constraint. Default to 256.
    :param bool action_scaling: whether to map actions from range [-1, 1] to range
        [action_spaces.low, action_spaces.high]. Default to True.
    :param str action_bound_method: method to bound action to range [-1, 1], can be
        either "clip" (for simply clipping the action), "tanh" (for applying tanh
        squashing) for now, or empty string for no bounding. Default to "clip".
    :param Optional[gym.Space] action_space: env's action space, mandatory if you want
        to use option "action_scaling" or "action_bound_method". Default to None.
    :param lr_scheduler: a learning rate scheduler that adjusts the learning rate in
        optimizer in each policy.update(). Default to None (no lr_scheduler).
    :param bool deterministic_eval: whether to use deterministic action instead of
        stochastic action sampled by the policy. Default to False.
    :param float kl_beta_of_pertrained_lang_model: approximated kl penalty reward
        to measure discrepancy between pretrained LMs

    .. seealso::

        Please refer to :class:`~tianshou.policy.BasePolicy` for more detailed
        explanation.
    """

    def __init__(
        self,
        actor: torch.nn.Module,
        critic: torch.nn.Module,
        optim: torch.optim.Optimizer,
        dist_fn: Type[torch.distributions.Distribution],
        eps_clip: float = 0.2,
        dual_clip: Optional[float] = None,
        value_clip: bool = False,
        advantage_normalization: bool = True,
        recompute_advantage: bool = False,
        kl_beta_of_pertrained_lang_model:AdaptiveKLController = AdaptiveKLController(100, 10, 1024, 0.2),
        kl_safe_delta:Optional[float]=None,
        **kwargs: Any,
    ) -> None:
        super().__init__(actor, critic, optim, dist_fn, **kwargs)
        self._eps_clip = eps_clip
        assert dual_clip is None or dual_clip > 1.0, \
            "Dual-clip PPO parameter should greater than 1.0."
        self._dual_clip = dual_clip
        self._value_clip = value_clip
        self._norm_adv = advantage_normalization
        self._recompute_adv = recompute_advantage
        self._actor_critic: ActorCritic
        self.kl_beta = kl_beta_of_pertrained_lang_model
        self.kl_safe_delta = kl_safe_delta

    def process_fn(
        self, batch: Batch, buffer: ReplayBuffer, indices: np.ndarray
    ) -> Batch:
        # print(batch.act.shape, batch.obs.shape, batch.rew.shape)
        #v batch.to_torch(dtype=None, device=self.actor.device)
        # print(batch.obs.shape, batch.act.shape, batch.act.dtype, batch.rew.shape)
        with torch.inference_mode():
            actor_out:Batch = self(batch)
            batch.act = to_torch(batch.act, device=actor_out.logits.device)
            batch.logp_old = actor_out.dist.log_prob(batch.act.squeeze(-1)).clone()
            
        if len(batch.act.shape) == 2 and batch.act.shape[-1] == 1:
            batch.act = batch.act.squeeze(-1)
        elif len(batch.act.shape) == 1:
            assert batch.act.shape[0] == batch.rew.shape[0] == batch.obs.shape[0]\
                , [batch.act.shape, batch.rew.shape, batch.obs.shape]
            
        if not hasattr(batch.info, "current_action_logits_of_initial_model"):
            raise ValueError("env must return info with the key *current_action_logits_of_initial_model* for this env.")

        ref_logits_old = to_torch(batch.info.current_action_logits_of_initial_model, device=actor_out.logits.device)
        # ref_logits_old = batch.info.current_action_logits_of_initial_model
        ref_logp_old = Categorical(logits=ref_logits_old).log_prob(batch.act)
        # assert False, [batch.logp_old.shape, ref_logp_old.shape, ref_logits_old.shape, batch.act.shape]
        # Initial Language Model as reference model
        self.approximate_kl_penal = -1 * self.kl_beta.value * (batch.logp_old - ref_logp_old)
        # assert False, [batch.rew.shape, self.approximate_kl_penal.cpu().numpy().shape]
        kl_penal = self.approximate_kl_penal.cpu().numpy()
        
        if self.kl_safe_delta is None:
            batch.rew = batch.rew + kl_penal
        else:
            # Excessive Penalty is forced to be zero.
            batch.rew = batch.rew + np.where(kl_penal > -1 * self.kl_safe_delta, 0.0, 1.0) * kl_penal

        if self._recompute_adv:
            # buffer input `buffer` and `indices` to be used in `learn()`.
            self._buffer, self._indices = buffer, indices

        batch = self._compute_returns(batch, buffer, indices)

        return batch

    def learn(  # type: ignore
        self, batch: Batch, batch_size: int, repeat: int, **kwargs: Any
    ) -> Dict[str, List[float]]:
        losses, clip_losses, vf_losses, ent_losses = [], [], [], []
        for step in range(repeat):
            if self._recompute_adv and step > 0:
                batch = self._compute_returns(batch, self._buffer, self._indices)
            for minibatch in batch.split(batch_size, merge_last=True):
                # calculate loss for actor
                dist = self(minibatch).dist
                if self._norm_adv:
                    mean, std = minibatch.adv.mean(), minibatch.adv.std()
                    minibatch.adv = (minibatch.adv -
                                     mean) / (std + self._eps)  # per-batch norm
                ratio = (dist.log_prob(minibatch.act) -
                         minibatch.logp_old).exp().float()
                ratio = ratio.reshape(ratio.size(0), -1).transpose(0, 1)
                surr1 = ratio * minibatch.adv
                surr2 = ratio.clamp(
                    1.0 - self._eps_clip, 1.0 + self._eps_clip
                ) * minibatch.adv
                if self._dual_clip:
                    clip1 = torch.min(surr1, surr2)
                    clip2 = torch.max(clip1, self._dual_clip * minibatch.adv)
                    clip_loss = -torch.where(minibatch.adv < 0, clip2, clip1).mean()
                else:
                    clip_loss = -torch.min(surr1, surr2).mean()
                # calculate loss for critic
                value = self.critic(minibatch.obs).flatten()
                if self._value_clip:
                    v_clip = minibatch.v_s + \
                        (value - minibatch.v_s).clamp(-self._eps_clip, self._eps_clip)
                    vf1 = (minibatch.returns - value).pow(2)
                    vf2 = (minibatch.returns - v_clip).pow(2)
                    vf_loss = torch.max(vf1, vf2).mean()
                else:
                    vf_loss = (minibatch.returns - value).pow(2).mean()
                # calculate regularization and overall loss
                ent_loss = dist.entropy().mean()
                loss = clip_loss + self._weight_vf * vf_loss \
                    - self._weight_ent * ent_loss
                self.optim.zero_grad()
                loss.backward()
                if self._grad_norm:  # clip large gradient
                    nn.utils.clip_grad_norm_(
                        self._actor_critic.parameters(), max_norm=self._grad_norm
                    )
                self.optim.step()
                clip_losses.append(clip_loss.item())
                vf_losses.append(vf_loss.item())
                ent_losses.append(ent_loss.item())
                losses.append(loss.item())

        self.kl_beta.update(self.approximate_kl_penal, batch_size)

        return {
            "loss": losses,
            "loss/clip": clip_losses,
            "loss/vf": vf_losses,
            "loss/ent": ent_losses,
        }

    def __learn(  # type: ignore
        self, batch: Batch, batch_size: int, repeat: int, **kwargs: Any
    ) -> Dict[str, List[float]]:
        losses, clip_losses, vf_losses, ent_losses = [], [], [], []
        for step in range(repeat):
            if self._recompute_adv and step > 0:
                batch = self._compute_returns(batch, self._buffer, self._indices)
                
            self.optim.zero_grad()
            _len = len([ _ for _ in batch.split(batch_size, merge_last=True)])
            for minibatch in batch.split(batch_size, merge_last=True):
                # calculate loss for actor
                dist = self(minibatch).dist
                if self._norm_adv:
                    mean, std = minibatch.adv.mean(), minibatch.adv.std()
                    minibatch.adv = (minibatch.adv -
                                     mean) / (std + self._eps)  # per-batch norm
                ratio = (dist.log_prob(minibatch.act) -
                         minibatch.logp_old).exp().float()
                ratio = ratio.reshape(ratio.size(0), -1).transpose(0, 1)
                surr1 = ratio * minibatch.adv
                surr2 = ratio.clamp(
                    1.0 - self._eps_clip, 1.0 + self._eps_clip
                ) * minibatch.adv
                if self._dual_clip:
                    clip1 = torch.min(surr1, surr2)
                    clip2 = torch.max(clip1, self._dual_clip * minibatch.adv)
                    clip_loss = -torch.where(minibatch.adv < 0, clip2, clip1).mean()
                else:
                    clip_loss = -torch.min(surr1, surr2).mean()
                # calculate loss for critic
                value = self.critic(minibatch.obs).flatten()
                if self._value_clip:
                    v_clip = minibatch.v_s + \
                        (value - minibatch.v_s).clamp(-self._eps_clip, self._eps_clip)
                    vf1 = (minibatch.returns - value).pow(2)
                    vf2 = (minibatch.returns - v_clip).pow(2)
                    vf_loss = torch.max(vf1, vf2).mean()
                else:
                    vf_loss = (minibatch.returns - value).pow(2).mean()
                # calculate regularization and overall loss
                ent_loss = dist.entropy().mean()
                loss = clip_loss + self._weight_vf * vf_loss \
                    - self._weight_ent * ent_loss
                
                (loss/_len).backward()

                clip_losses.append(clip_loss.item())
                vf_losses.append(vf_loss.item())
                ent_losses.append(ent_loss.item())
                losses.append(loss.item())
                
            if self._grad_norm:  # clip large gradient
                nn.utils.clip_grad_norm_(
                    self._actor_critic.parameters(), max_norm=self._grad_norm
                )
            self.optim.step()
        


        return {
            "loss": losses,
            "loss/clip": clip_losses,
            "loss/vf": vf_losses,
            "loss/ent": ent_losses,
        }