"""
.. module:: mcmc
   :synopsis: MCMC sampler
.. moduleauthor:: Adam Moss <adam.moss@nottingham.ac.uk>
Performs MCMC to obtain posterior samples
"""

from __future__ import print_function
from __future__ import division

import os
import glob

import torch
import numpy as np
from getdist.mcsamples import MCSamples
from getdist.chains import chainFiles
from tqdm import tqdm
import matplotlib
import emcee

matplotlib.use('Agg')

from nnest.sampler import Sampler
from nnest.utils.evaluation import acceptance_rate, effective_sample_size, mean_jump_distance


class MCMCSampler(Sampler):

    def __init__(self,
                 x_dim,
                 loglike,
                 transform=None,
                 append_run_num=True,
                 run_num=None,
                 hidden_dim=128,
                 num_slow=0,
                 num_derived=0,
                 batch_size=100,
                 flow='nvp',
                 num_blocks=5,
                 num_layers=2,
                 log_dir='logs/test',
                 base_dist=None,
                 scale='',
                 use_gpu=False,
                 trainer=None,
                 oversample_rate=-1):

        super(MCMCSampler, self).__init__(x_dim, loglike, transform=transform, append_run_num=append_run_num,
                                          run_num=run_num, hidden_dim=hidden_dim, num_slow=num_slow,
                                          num_derived=num_derived, batch_size=batch_size, flow=flow,
                                          num_blocks=num_blocks, num_layers=num_layers, log_dir=log_dir,
                                          use_gpu=use_gpu, base_dist=base_dist, scale=scale, trainer=trainer)

        self.sampler = 'mcmc'
        self.oversample_rate = oversample_rate if oversample_rate > 0 else self.num_fast / self.x_dim

    def mcmc_sample(
            self,
            mcmc_steps=20,
            stepsize=1.0,
            dynamic_stepsize=False,
            batch_size=1,
            init_x=None,
            loglstar=None,
            show_progress=False,
            out_chain=None,
            max_prior=None,
            max_start_tries=100,
            output_interval=0,
            stats_interval=0):

        self.trainer.netG.eval()

        samples = []
        derived_samples = []
        likes = []

        if init_x is not None:
            batch_size = init_x.shape[0]
            z, _ = self.trainer.netG(torch.from_numpy(init_x).float().to(self.trainer.device))
            z = z.detach()
            # Add the backward version of x rather than init_x due to numerical precision
            x, _ = self.trainer.netG(z, mode='inverse')
            x = x.detach().cpu().numpy()
            logl, derived = self.loglike(self.transform(x))
        else:
            for i in range(max_start_tries):
                z = torch.randn(batch_size, self.trainer.z_dim, device=self.trainer.device)
                x, _ = self.trainer.netG(z, mode='inverse')
                x = x.detach().cpu().numpy()
                logl, derived = self.loglike(self.transform(x))
                if np.all(logl > -1e30):
                    break
                if i == max_start_tries - 1:
                    raise Exception('Could not find starting value')

        samples.append(x)
        derived_samples.append(derived)
        likes.append(logl)

        iters = tqdm(range(mcmc_steps)) if show_progress else range(mcmc_steps)
        scale = stepsize
        accept = 0
        reject = 0
        ncall = 0

        if out_chain is not None:
            if batch_size == 1:
                files = [open(out_chain + '.txt', 'w')]
            else:
                files = [open(out_chain + '_%s.txt' % (ib + 1), 'w') for ib in range(batch_size)]

        for it in iters:

            dz = torch.randn_like(z) * scale
            if self.num_slow > 0 and np.random.uniform() < self.oversample_rate:
                fast = True
                dz[:, 0:self.num_slow] = 0.0
            else:
                fast = False
            z_prime = z + dz

            # Jacobian is det d f^{-1} (z)/dz
            x, log_det_J = self.trainer.netG(z, mode='inverse')
            x_prime, log_det_J_prime = self.trainer.netG(z_prime, mode='inverse')
            x = x.detach().cpu().numpy()
            x_prime = x_prime.detach().cpu().numpy()
            delta_log_det_J = (log_det_J_prime - log_det_J).detach()
            log_ratio_1 = delta_log_det_J.squeeze(dim=1)

            # Check not out of prior range
            if max_prior is not None:
                prior = np.logical_or(np.abs(x) > max_prior, np.abs(x_prime) > max_prior)
                idx = np.where([np.any(p) for p in prior])
                log_ratio_1[idx] = -np.inf

            rnd_u = torch.rand(log_ratio_1.shape, device=self.trainer.device)
            ratio = log_ratio_1.exp().clamp(max=1)
            mask = (rnd_u < ratio).int()
            logl_prime = np.full(batch_size, logl)
            derived_prime = np.copy(derived)

            # Only evaluate likelihood if prior and volume is accepted
            if self.loglike is not None and self.transform is not None:
                for idx, im in enumerate(mask):
                    if im:
                        if not fast:
                            ncall += 1
                        lp, der = self.loglike(self.transform(x_prime[idx]))
                        if loglstar is not None:
                            if np.isfinite(lp) and lp >= loglstar:
                                logl_prime[idx] = lp
                                derived_prime[idx] = der
                            else:
                                mask[idx] = 0
                        else:
                            if lp >= logl[idx]:
                                logl_prime[idx] = lp
                            elif rnd_u[idx].cpu().numpy() < np.clip(np.exp(lp - logl[idx]), 0, 1):
                                logl_prime[idx] = lp
                                derived_prime[idx] = der
                            else:
                                mask[idx] = 0

            if 2 * torch.sum(mask).cpu().numpy() > batch_size:
                accept += 1
            else:
                reject += 1

            if dynamic_stepsize:
                if accept > reject:
                    scale *= np.exp(1. / accept)
                if accept < reject:
                    scale /= np.exp(1. / reject)

            m = mask[:, None].float()
            z = (z_prime * m + z * (1 - m)).detach()
            derived = derived_prime * m.cpu().numpy() + derived * (1 - m.cpu().numpy())

            m = mask
            logl = logl_prime * m.cpu().numpy() + logl * (1 - m.cpu().numpy())

            x, _ = self.trainer.netG(z, mode='inverse')
            x = x.detach().cpu().numpy()
            samples.append(x)
            derived_samples.append(derived)
            likes.append(logl)

            if output_interval > 0 and it % output_interval == 0 and out_chain is not None:
                self._save_samples(np.transpose(np.array(self.transform(samples)), axes=[1, 0, 2]),
                                   np.transpose(np.array(likes), axes=[1, 0]),
                                   derived_samples=np.transpose(np.array(derived_samples), axes=[1, 0, 2]))

            if stats_interval > 0 and it % stats_interval == 0:
                self._chain_stats(np.transpose(np.array(samples), axes=[1, 0, 2]))

        # Transpose samples so shape is (chain_num, iteration, dim)
        samples = np.transpose(np.array(samples), axes=[1, 0, 2])
        derived_samples = np.transpose(np.array(derived_samples), axes=[1, 0, 2])
        likes = np.transpose(np.array(likes), axes=[1, 0])

        if out_chain is not None:
            for ib in range(batch_size):
                files[ib].close()

        return samples, derived_samples, likes, scale, ncall

    def _chain_stats(self, samples, mean=None, std=None):
        acceptance = acceptance_rate(samples)
        if mean is None:
            mean = np.mean(np.reshape(samples, (-1, samples.shape[2])), axis=0)
        if std is None:
            std = np.std(np.reshape(samples, (-1, samples.shape[2])), axis=0)
        ess = effective_sample_size(samples, mean, std)
        jump_distance = mean_jump_distance(samples)
        self.logger.info(
            'Acceptance [%5.4f] min ESS [%5.4f] max ESS [%5.4f] average jump distance [%5.4f]' %
            (acceptance, np.min(ess), np.max(ess), jump_distance))
        return acceptance, ess, jump_distance

    def _save_samples(self, samples, loglikes, weights=None, derived_samples=None, min_weight=1e-30, outfile='chain'):
        if weights is None:
            weights = np.ones_like(loglikes)
        if len(samples.shape) == 2:
            # Single chain
            with open(os.path.join(self.logs['chains'], outfile + '.txt'), 'w') as f:
                for i in range(samples.shape[0]):
                    f.write("%.5E " % max(weights[i], min_weight))
                    f.write("%.5E " % -loglikes[i])
                    f.write(" ".join(["%.5E" % v for v in samples[i, :]]))
                    if derived_samples is not None:
                        f.write(" ")
                        f.write(" ".join(["%.5E" % v for v in derived_samples[i, :]]))
                    f.write("\n")
        elif len(samples.shape) == 3:
            # Multiple chains
            for ib in range(samples.shape[0]):
                with open(os.path.join(self.logs['chains'], outfile + '_%s.txt' % (ib + 1)), 'w') as f:
                    for i in range(samples.shape[1]):
                        f.write("%.5E " % max(weights[ib, i], min_weight))
                        f.write("%.5E " % -loglikes[ib, i])
                        f.write(" ".join(["%.5E" % v for v in samples[ib, i, :]]))
                        if derived_samples is not None:
                            f.write(" ")
                            f.write(" ".join(["%.5E" % v for v in derived_samples[ib, i, :]]))
                        f.write("\n")

    def _init_samples(self, mcmc_steps=100, nwalkers=10, burn_in_steps=10):
        self.logger.info('Getting initial samples with emcee')
        p0 = np.random.rand(nwalkers, self.x_dim)
        sampler = emcee.EnsembleSampler(nwalkers, self.x_dim, self.loglike)
        state = sampler.run_mcmc(p0, burn_in_steps)
        sampler.reset()
        sampler.run_mcmc(state, mcmc_steps)
        self.logger.info('Mean acceptance fraction: [%5.3f]'  % np.mean(sampler.acceptance_fraction))
        mc = MCSamples(samples=sampler.get_chain())
        return mc

    def _read_samples(self, fileroot, match='', ignore_rows=0.3):
        names = ['p%i' % i for i in range(int(self.num_params))]
        labels = [r'x_%i' % i for i in range(int(self.num_params))]
        if match:
            files = glob.glob(os.path.join(fileroot, match))
        else:
            files = chainFiles(fileroot)
        mc = MCSamples(fileroot, names=names, labels=labels, ignore_rows=ignore_rows)
        mc.readChains(files)
        return mc

    def run(
            self,
            train_iters=200,
            mcmc_steps=5000,
            bootstrap_iters=1,
            bootstrap_mcmc_steps=5000,
            bootstrap_fileroot='',
            bootstrap_match='',
            bootstrap_batch_size=5,
            stepsize=0,
            single_thin=1,
            ignore_rows=0.3,
            stats_interval=100,
            output_interval=100):

        if stepsize == 0.0:
            stepsize = 1 / self.x_dim ** 0.5

        if self.log:
            self.logger.info('Alpha [%5.4f]' % (stepsize))

        for t in range(bootstrap_iters):

            if t == 0:
                if bootstrap_fileroot:
                    mc = self._read_samples(bootstrap_fileroot, match=bootstrap_match, ignore_rows=ignore_rows)
                else:
                    mc = self._init_samples()
            else:
                samples, derived_samples, likes, scale, nc = self.mcmc_sample(
                    mcmc_steps=bootstrap_mcmc_steps, stepsize=stepsize, dynamic=False, show_progress=True)
                samples = self.transform(samples)
                self._chain_stats(samples)
                mc = MCSamples(samples=samples, ignore_rows=ignore_rows)

            samples = mc.makeSingleSamples(single_thin=single_thin)
            samples = samples[:, :self.x_dim]
            mean = np.mean(samples, axis=0)
            std = np.std(samples, axis=0)
            # Normalise samples
            samples = (samples - mean) / std
            self.trainer.train(samples, max_iters=train_iters, noise=-1)
            self.transform = lambda x: x * std + mean

        samples, derived_samples, likes, scale, nc = self.mcmc_sample(
            mcmc_steps=mcmc_steps, stepsize=stepsize, dynamic_stepsize=False, show_progress=True,
            out_chain=os.path.join(self.logs['chains'], 'chain'), stats_interval=stats_interval,
            output_interval=output_interval)
        samples = self.transform(samples)
        self._chain_stats(samples)

        self.samples = samples
        self.loglikes = -likes

