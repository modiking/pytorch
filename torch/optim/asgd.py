import math
import torch
from torch import Tensor

from .optimizer import Optimizer
from typing import List


class ASGD(Optimizer):
    """Implements Averaged Stochastic Gradient Descent.

    It has been proposed in `Acceleration of stochastic approximation by
    averaging`_.

    Args:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-2)
        lambd (float, optional): decay term (default: 1e-4)
        alpha (float, optional): power for eta update (default: 0.75)
        t0 (float, optional): point at which to start averaging (default: 1e6)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        foreach (bool, optional): whether foreach implementation of optimizer
            is used (default: False)

    .. _Acceleration of stochastic approximation by averaging:
        https://dl.acm.org/citation.cfm?id=131098
    """

    def __init__(self, params, lr=1e-2, lambd=1e-4, alpha=0.75, t0=1e6, weight_decay=0, foreach=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))

        defaults = dict(lr=lr, lambd=lambd, alpha=alpha, t0=t0,
                        weight_decay=weight_decay, foreach=foreach)
        super(ASGD, self).__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.

        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            params_with_grad = []
            grads = []
            mus = []
            axs = []
            etas = []
            state_steps = []

            for p in group['params']:
                if p.grad is not None:
                    params_with_grad.append(p)
                    if p.grad.is_sparse:
                        raise RuntimeError('ASGD does not support sparse gradients')
                    grads.append(p.grad)

                    state = self.state[p]
                    # State initialization
                    if len(state) == 0:
                        state['step'] = 0
                        state['eta'] = group['lr']
                        state['mu'] = 1
                        state['ax'] = torch.zeros_like(p, memory_format=torch.preserve_format)

                    mus.append(state['mu'])
                    axs.append(state['ax'])
                    etas.append(state['eta'])

                    state['step'] += 1
                    state_steps.append(state['step'])

            asgd(params_with_grad,
                 grads,
                 axs,
                 mus,
                 etas,
                 weight_decay=group['weight_decay'],
                 lambd=group['lambd'],
                 foreach=group['foreach'])

            # update eta and mu
            for p in params_with_grad:
                state = self.state[p]
                state['eta'] = (group['lr'] /
                                math.pow((1 + group['lambd'] * group['lr'] * state['step']), group['alpha']))
                state['mu'] = 1 / max(1, state['step'] - group['t0'])

        return loss


def asgd(params: List[Tensor],
         grads: List[Tensor],
         axs: List[Tensor],
         mus: List[float],
         etas: List[float],
         *,
         weight_decay: float,
         lambd: float,
         foreach: bool):
    r"""Functional API that performs asgd algorithm computation.

    See :class:`~torch.optim.ASGD` for details.
    """

    if foreach and not torch.jit.is_scripting():
        func = _multi_tensor_asgd
    else:
        func = _single_tensor_asgd

    func(params,
         grads,
         axs,
         mus,
         etas,
         weight_decay=weight_decay,
         lambd=lambd)


def _single_tensor_asgd(params: List[Tensor],
                        grads: List[Tensor],
                        axs: List[Tensor],
                        mus: List[float],
                        etas: List[float],
                        *,
                        weight_decay: float,
                        lambd: float):

    for i, param in enumerate(params):
        grad = grads[i]
        mu = mus[i]
        ax = axs[i]
        eta = etas[i]

        if weight_decay != 0:
            grad = grad.add(param, alpha=weight_decay)

        # decay term
        param.mul_(1 - lambd * eta)

        # update parameter
        param.add_(grad, alpha=-eta)

        # averaging
        if mu != 1:
            ax.add_(param.sub(ax).mul(mu))
        else:
            ax.copy_(param)


def _multi_tensor_asgd(params: List[Tensor],
                       grads: List[Tensor],
                       axs: List[Tensor],
                       mus: List[float],
                       etas: List[float],
                       *,
                       weight_decay: float,
                       lambd: float):


    if weight_decay != 0:
        torch._foreach_add_(grads, params, alpha=weight_decay)

    # decay term
    decay = [1 - lambd * eta for eta in etas]
    torch._foreach_mul_(params, decay)

    # update parameter
    updates = torch._foreach_mul(grads, etas)
    torch._foreach_add_(params, updates, alpha=-1)

    # averaging
    for i, mu in enumerate(mus):
        if mu != 1:
            axs[i].add_(params[i].sub(axs[i]).mul(mu))
        else:
            axs[i].copy_(params[i])
