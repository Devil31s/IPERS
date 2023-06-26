import copy
from components.episode_buffer import EpisodeBatch

from modules.mixers.vdn import VDNMixer
from modules.mixers.qmix import QMixer
import torch as th
from torch.optim import RMSprop
import pdb
import torch.nn.functional as F
import numpy as np
import torch.nn as nn
from ER.PER.prioritized_memory import PER_Memory


class maserQDivideLearnerNEW:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.mac = mac

        self.logger = logger
        self.n_agents = args.n_agents
        self.device = args.device
        self.params = list(mac.parameters())

        self.last_target_update_episode = 0
        self.lam = args.lam
        self.alpha = args.alpha
        self.ind = args.ind
        self.mix = args.mix
        self.expl = args.expl
        self.dis = args.dis
        self.goal = args.goal
        self.mixer = None
        if args.mixer is not None:
            if args.mixer == "vdn":
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                self.mixer = QMixer(args)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())
            self.target_mixer = copy.deepcopy(self.mixer)

        self.optimiser = RMSprop(params=self.params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps)
        self.q_params = list(mac.parameters())
        self.q_optimiser = RMSprop(params=self.q_params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps)

        # a little wasteful to deepcopy (e.g. duplicates action selector), but should work for any MAC
        self.target_mac = copy.deepcopy(mac)

        self.log_stats_t = -self.args.learner_log_interval - 1

        # self.distance = nn.Linear(self.mac.scheme1['obs']['vshape'], args.n_actions).to(device=self.device)

        self.distance = nn.Sequential(
            nn.Linear(self.mac.scheme1['obs']['vshape'], 128),
            nn.ReLU(),
            nn.Linear(128, args.n_actions)
        ).to(device=self.device)

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities

        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :-1]
        observation = batch["obs"][:, :-1]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"]
        indi_terminated = batch["indi_terminated"][:, :-1].float()

        # Calculate estimated Q-Values
        mac_out = []
        self.mac.init_hidden(batch.batch_size)

        for t in range(batch.max_seq_length):
            agent_outs = self.mac.forward(batch, t=t)  # (bs,n,n_actions)
            mac_out.append(agent_outs)  # [t,(bs,n,n_actions)]
        mac_out = th.stack(mac_out, dim=1)  # Concat over time

        # Pick the Q-Values for the actions taken by each agent

        chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)  # Remove the last dim
        ind_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)
        # (bs,t,n) Q value of an action

        # Calculate the Q-Values necessary for the target
        target_mac_out = []
        self.target_mac.init_hidden(batch.batch_size)  # (bs,n,hidden_size)
        for t in range(batch.max_seq_length):
            target_agent_outs = self.target_mac.forward(batch, t=t)  # (bs,n,n_actions)
            target_mac_out.append(target_agent_outs)  # [t,(bs,n,n_actions)]

        # We don't need the first timesteps Q-Value estimate for calculating targets
        target_ind_q = th.stack(target_mac_out[:-1], dim=1)  #### For Q value s
        target_mac_out = th.stack(target_mac_out[1:],
                                  dim=1)  # Concat across time, dim=1 is time index #####For target s'

        # (bs,t,n,n_actions)

        # Mask out unavailable actions
        target_mac_out[avail_actions[:, 1:] == 0] = -9999999  # Q values
        target_ind_q[avail_actions[:, :-1] == 0] = -9999999  # Q values

        # Max over target Q-Values
        if self.args.double_q:  # True for QMix
            # Get actions that maximise live Q (for double q-learning)
            mac_out_detach = mac_out.clone().detach()  # return a new Tensor, detached from the current graph
            mac_out_detach[avail_actions == 0] = -9999999
            # (bs,t,n,n_actions), discard t=0
            cur_max_actions = mac_out_detach[:, 1:].max(dim=3, keepdim=True)[1]  # indices instead of values
            cur_max_act = mac_out_detach[:, :-1].max(dim=3, keepdim=True)[1]  # indices instead of values
            # (bs,t,n,1)
            target_max_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
            target_individual_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
            target_ind_qvals = th.gather(target_ind_q, 3, cur_max_act).squeeze(3)
            # (bs,t,n,n_actions) ==> (bs,t,n,1) ==> (bs,t,n) max target-Q
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]
            target_individual_qvals = target_mac_out.max(dim=3)[0]
        # Mix

        if self.mixer is not None:
            chosen_action_qvals_clone = chosen_action_qvals.clone().detach()
            chosen_action_qvals_clone.requires_grad = True
            target_max_qvals_clone = target_max_qvals.clone().detach()

            chosen_action_q_tot_vals = self.mixer(chosen_action_qvals_clone, batch["state"][:, :-1])
            target_max_q_tot_vals = self.target_mixer(target_max_qvals_clone, batch["state"][:, 1:])
            goal_target_max_qvals = self.target_mixer(target_ind_qvals, batch["state"][:,
                                                                        :-1])  # target_ind_qvals / ddqn_qval_up same result
            # (bs,t,1)

        ##############################################################################################################################
        if self.goal == "maser":

            q_ind_tot_list = []
            for i in range(self.n_agents):
                target_qtot_per_agent = (goal_target_max_qvals / self.n_agents).squeeze()
                q_ind_tot_list.append(self.alpha * target_ind_qvals[:, :, i] + (1 - self.alpha) * target_qtot_per_agent)

            q_ind_tot = th.stack(q_ind_tot_list, dim=2)

            ddqn_qval_up_idx = th.max(q_ind_tot, dim=1)[1]  # Find out max Q value for t=1~T-1 (whole episode)

            explore_q_target = th.ones(target_ind_q.shape) / target_ind_q.shape[-1]
            explore_q_target = explore_q_target.to(device=self.device)

            ddqn_up_list = []
            distance_list = []
            for i in range(batch.batch_size):
                ddqn_up_list_subset = []
                distance_subset = []
                explore_loss_subset = []
                for j in range(self.n_agents):
                    # For distance function

                    cos = nn.CosineSimilarity(dim=-1, eps=1e-8)
                    cos1 = nn.CosineSimilarity()
                    goal_q = target_ind_q[i, ddqn_qval_up_idx[i][j], j, :].repeat(target_ind_q.shape[1], 1)

                    a = cos(target_ind_q[i, :, j, :], goal_q)
                    b = cos1(target_ind_q[i, :, j, :], goal_q)

                    similarity = 1 - cos(target_ind_q[i, :, j, :], goal_q)
                    dist_obs = self.distance(observation[i, :, j, :])
                    dist_og = self.distance(observation[i, ddqn_qval_up_idx[i][j], j, :])

                    dist_loss = th.norm(dist_obs - dist_og.repeat(dist_obs.shape[0], 1), dim=-1) - similarity
                    distance_loss = th.mean(dist_loss ** 2)
                    distance_subset.append(distance_loss)
                    ddqn_up_list_subset.append(observation[i, ddqn_qval_up_idx[i][j], j, :])

                distance1 = th.stack(distance_subset)
                distance_list.append(distance1)

                ddqn_up1 = th.stack(ddqn_up_list_subset)
                ddqn_up_list.append(ddqn_up1)

            distance_losses = th.stack(distance_list)

            mix_explore_distance_losses = self.dis * distance_losses

            ddqn_up = th.stack(ddqn_up_list)
            ddqn_up = ddqn_up.unsqueeze(dim=1)
            ddqn_up = ddqn_up.repeat(1, observation.shape[1], 1, 1)

            reward_ddqn_up = self.distance(observation) - self.distance(ddqn_up)

            intrinsic_reward_list = []
            for i in range(self.n_agents):
                intrinsic_reward_list.append(
                    -th.norm(reward_ddqn_up[:, :, i, :], dim=2).reshape(batch.batch_size, observation.shape[1]))
            intrinsic_rewards_ind = th.stack(intrinsic_reward_list, dim=-1)

            intrinsic_rewards = th.zeros(rewards.shape).to(device=self.device)

            for i in range(self.n_agents):
                intrinsic_rewards += -th.norm(reward_ddqn_up[:, :, i, :], dim=2).reshape(batch.batch_size,
                                                                                         observation.shape[1],
                                                                                         1) / self.n_agents
            rewards_tot = rewards + self.lam * intrinsic_rewards

        ###################################################################################################################
        # Calculate 1-step Q-Learning targets

        targets = rewards_tot + self.args.gamma * (1 - terminated) * target_max_q_tot_vals

        # Td-error
        td_error = (chosen_action_q_tot_vals - targets.detach())  # no gradient through target net
        # (bs,t,1)

        # distillation-error

        mask = mask.expand_as(td_error)

        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask

        # Normal L2 loss, take mean over actual data
        loss = (masked_td_error ** 2).sum() / mask.sum()

        # grad_qtot_qi = th.clamp(grad_l_qi / grad_l_qtot, min=-10, max=10)  # (B,T,n_agents)
        # q_rewards = self.cal_indi_reward(grad_qtot_qi, td_error, chosen_action_qvals, target_max_qvals, indi_terminated)  # (B,T,n_agents)
        # q_rewards_clone = q_rewards.clone().detach()
        # q_rewards_clone += self.lam * intrinsic_rewards
        # q_targets = q_rewards_clone + self.args.gamma * (1 - indi_terminated) * target_max_qvals  # (B,T,n_agents)
        # # Td-error
        # q_td_error = (chosen_action_qvals - q_targets.detach())  # (B,T,n_agents)
        # q_mask = batch["filled"][:, :-1].float().repeat(1, 1, self.args.n_agents)  # (B,T,n_agents)
        # q_mask[:, 1:] = q_mask[:, 1:] * (1 - indi_terminated[:, :-1]) * (1 - terminated[:, :-1]).repeat(1, 1, self.args.n_agents)
        # q_mask = q_mask.expand_as(q_td_error)
        # masked_q_td_error = q_td_error * q_mask
        # q_selected_weight, selected_ratio = self.select_trajectory(masked_q_td_error.abs(), q_mask, t_env)
        # q_selected_weight = q_selected_weight.clone().detach()
        # q_loss = th.sum(masked_q_td_error ** 2 * q_selected_weight).sum() / th.mean(q_mask, dim=-1).sum()
        # y = F.softmax(target_individual_qvals, dim=-1)
        # r_i = y * rewards + self.lam * intrinsic_rewards_ind
        # individual_targets = y * rewards + self.lam * intrinsic_rewards_ind + self.args.gamma * (
        #             1 - terminated.repeat(1, 1, target_individual_qvals.shape[-1])) * target_individual_qvals
        # td_individual_error = (ind_qvals - individual_targets.detach())
        # ind_mask = mask.expand_as(td_individual_error)
        # masked_td_individual_error = td_individual_error * ind_mask
        # q_selected_weight, selected_ratio = self.select_trajectory(masked_td_individual_error.abs(), ind_mask,
        #                                                            t_env)
        # q_selected_weight = q_selected_weight.clone().detach()
        # print((q_selected_weight * ind_mask).sum().item() / (ind_mask.sum().item()))
        # individual_loss = th.sum(masked_td_individual_error ** 2 * q_selected_weight).sum() / th.mean(ind_mask,
        #                                                                                               dim=-1).sum()
        mix_explore_distance_loss = mix_explore_distance_losses.mean()
        loss += 0.001 * (self.mix * mix_explore_distance_loss)

        self.optimiser.zero_grad()
        chosen_action_qvals_clone.retain_grad()  # the grad of qi
        chosen_action_q_tot_vals.retain_grad()  # the grad of qtot
        loss.backward(retain_graph=True)
        grad_l_qtot = chosen_action_q_tot_vals.grad.repeat(1, 1, self.args.n_agents) + 1e-8
        grad_l_qi = chosen_action_qvals_clone.grad
        grad_qtot_qi = th.clamp(grad_l_qi / grad_l_qtot, min=-10, max=10)  # (B,T,n_agents)
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)  # max_norm
        self.optimiser.step()
        # print("chosen_action_qvals", chosen_action_qvals)
        # print("target_max_qvals is ,", target_max_qvals)
        q_rewards = self.cal_indi_reward(grad_qtot_qi, td_error, chosen_action_qvals, target_max_qvals,
                                         indi_terminated)  # (B,T,n_agents)
        q_rewards_clone = q_rewards.clone().detach()
        q_rewards_clone += self.lam * intrinsic_rewards
        q_targets = q_rewards_clone + self.args.gamma * (1 - indi_terminated) * target_max_qvals  # (B,T,n_agents)
        # Td-error
        q_td_error = (chosen_action_qvals_clone - q_targets.detach())  # (B,T,n_agents)
        q_mask = batch["filled"][:, :-1].float().repeat(1, 1, self.args.n_agents)  # (B,T,n_agents)
        q_mask[:, 1:] = q_mask[:, 1:] * (1 - indi_terminated[:, :-1]) * (1 - terminated[:, :-1]).repeat(1, 1,
                                                                                                        self.args.n_agents)
        q_mask = q_mask.expand_as(q_td_error)
        masked_q_td_error = q_td_error * q_mask
        q_selected_weight, selected_ratio = self.select_trajectory(masked_q_td_error.abs(), q_mask, t_env)
        q_selected_weight = q_selected_weight.clone().detach()
        q_loss = 0.001 * (masked_q_td_error ** 2 * q_selected_weight).sum() / q_mask.sum()

        self.q_optimiser.zero_grad()
        q_loss.backward()
        q_grad_norm = th.nn.utils.clip_grad_norm_(self.q_params, self.args.grad_norm_clip)
        self.q_optimiser.step()

        if (episode_num - self.last_target_update_episode) / self.args.target_update_interval >= 1.0:
            self._update_targets()
            self.last_target_update_episode = episode_num

        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            # q_mask_elems = q_mask.sum().item()
            mask_elems = mask.sum().item()
            q_mask_elems = q_mask.sum().item()
            self.logger.log_stat("q_selected_weight_mean",
                                 (q_selected_weight * q_mask).sum().item() / (q_mask_elems), t_env)
            self.logger.log_stat("loss", loss.item(), t_env)
            self.logger.log_stat("grad_norm", grad_norm, t_env)
            # mask_elems = mask.sum().item()
            self.logger.log_stat("td_error_abs", (masked_td_error.abs().sum().item() / mask_elems), t_env)
            self.logger.log_stat("q_taken_mean",
                                 (chosen_action_qvals * mask).sum().item() / (mask_elems * self.args.n_agents), t_env)
            self.logger.log_stat("target_mean", (targets * mask).sum().item() / (mask_elems * self.args.n_agents),
                                 t_env)
            self.log_stats_t = t_env

    def cal_indi_reward(self, grad_qtot_qi, mixer_td_error, qi, target_qi, indi_terminated):
        # input: grad_qtot_qi (B,T,n_agents)  mixer_td_error (B,T,1)  qi (B,T,n_agents)  indi_terminated (B,T,n_agents)
        grad_td = th.mul(grad_qtot_qi, mixer_td_error.repeat(1, 1, self.args.n_agents))  # (B,T,n_agents)
        reward_i = - grad_td + qi - self.args.gamma * (1 - indi_terminated) * target_qi
        return reward_i

    def select_trajectory(self, td_error, mask, t_env):
        # td_error (B, T, n_agents)
        if self.args.warm_up:
            if t_env / self.args.t_max <= self.args.warm_up_ratio:
                selected_ratio = t_env * (self.args.selected_ratio_end - self.args.selected_ratio_start) / (
                        self.args.t_max * self.args.warm_up_ratio) + self.args.selected_ratio_start
            else:
                selected_ratio = self.args.selected_ratio_end
        else:
            selected_ratio = self.args.selected_ratio

        if self.args.selected == 'all':
            return th.ones_like(td_error).cuda(), selected_ratio
        elif self.args.selected == 'greedy':
            valid_num = mask.sum().item()
            selected_num = int(valid_num * selected_ratio)
            td_reshape = td_error.reshape(-1)
            sorted_td, _ = th.topk(td_reshape, selected_num)
            pivot = sorted_td[-1]
            weight = th.where(td_error >= pivot, th.ones_like(td_error), th.zeros_like(td_error))
            return weight, selected_ratio
        elif self.args.selected == 'greedy_weight':
            valid_num = mask.sum().item()
            selected_num = int(valid_num * selected_ratio)
            td_reshape = td_error.reshape(-1)
            sorted_td, _ = th.topk(td_reshape, selected_num)
            pivot = sorted_td[-1]
            weight = th.where(td_error >= pivot, td_error - pivot, th.zeros_like(td_error))
            norm_weight = weight / weight.max()
            return norm_weight, selected_ratio
        elif self.args.selected == 'PER':
            memory_size = int(mask.sum().item())
            memory = PER_Memory(memory_size)
            for b in range(mask.shape[0]):
                for t in range(mask.shape[1]):
                    for na in range(mask.shape[2]):
                        pos = (b, t, na)
                        if mask[pos] == 1:
                            memory.store(td_error[pos].cpu().detach(), pos)
            selected_num = int(memory_size * selected_ratio)
            mini_batch, selected_pos, is_weight = memory.sample(selected_num)
            weight = th.zeros_like(td_error)
            for idxs, pos in enumerate(selected_pos):
                weight[pos] += is_weight[idxs]
            return weight, selected_ratio
        elif self.args.selected == 'PER_hard':
            memory_size = int(mask.sum().item())
            selected_num = int(memory_size * selected_ratio)
            return PER_Memory(self.args, td_error, mask).sample(selected_num), selected_ratio
        elif self.args.selected == 'PER_weight':
            memory_size = int(mask.sum().item())
            selected_num = int(memory_size * selected_ratio)
            return PER_Memory(self.args, td_error, mask).sample_weight(selected_num, t_env), selected_ratio

    def _update_targets(self):
        self.target_mac.load_state(self.mac)
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())
        self.logger.console_logger.info("Updated target network")

    def cuda(self):
        self.mac.cuda()
        self.target_mac.cuda()
        if self.mixer is not None:
            self.mixer.cuda()
            self.target_mixer.cuda()

    def save_models(self, path):
        self.mac.save_models(path)
        if self.mixer is not None:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.q_optimiser.state_dict(), "{}/q_opt.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path):
        self.mac.load_models(path)
        # Not quite right but I don't want to save target networks
        self.target_mac.load_models(path)
        if self.mixer is not None:
            self.mixer.load_state_dict(th.load("{}/mixer.th".format(path), map_location=lambda storage, loc: storage))
        self.q_optimiser.load_state_dict(th.load("{}/q_opt.th".format(path), map_location=lambda storage, loc: storage))
        self.optimiser.load_state_dict(th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage))
