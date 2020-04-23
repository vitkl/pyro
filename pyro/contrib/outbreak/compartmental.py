# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import logging
import re
from abc import ABC, abstractmethod

import torch
from torch.distributions import biject_to, constraints
from torch.nn.functional import pad

import pyro.distributions as dist
import pyro.distributions.hmm
import pyro.poutine as poutine
from pyro.distributions.transforms import DiscreteCosineTransform
from pyro.infer import MCMC, NUTS, infer_discrete
from pyro.infer.autoguide import init_to_value
from pyro.infer.reparam import DiscreteCosineReparam
from pyro.util import warn_if_nan

from .util import quantize, quantize_enumerate

logger = logging.getLogger(__name__)


class CompartmentalModel(ABC):
    """
    Abstract base class for discrete-time discrete-value stochastic
    compartmental models.

    Derived classes must implement methods :meth:`heuristic`,
    :meth:`initialize`, :meth:`transition_fwd`, :meth:`transition_bwd`.
    Derived classes may optionally implement :meth:`global_model` and override
    the :cvar:`series` attribute.

    Example usage::

        # First implement a concrete derived class.
        class MyModel(CompartmentalModel):
            def __init__(self, ...): ...
            def heuristic(self): ...
            def global_model(self): ...
            def initialize(self, params): ...
            def transition_fwd(self, params, state, t): ...
            def transition_bwd(self, params, prev, curr): ...

        # Run inference to fit the model to data.
        model = MyModel(...)
        model.fit(num_samples=100)
        R0 = model.samples["R0"]  # An example parameter.
        print("R0 = {:0.3g} \u00B1 {:0.3g}".format(R0.mean(), R0.std()))

        # Predict latent variables.
        samples = model.predict()

        # Forecast forward.
        samples = model.predict(forecast=30)

        # You can assess future interventions (applied after ``duration``) by
        # storing them as attributes that are read by your derived methods.
        model.my_intervention = False
        samples1 = model.predict(forecast=30)
        model.my_intervention = True
        samples2 = model.predict(forecast=30)
        effect = samples2["my_result"].mean() - samples1["my_result"].mean()
        print("average effect = {:0.3g}".format(effect))

    :cvar tuple series: Tuple of names of time series names, in addition to
        ``self.compartments``. These will be concatenated along the time axis
        in returned sample dictionaries.
    :ivar dict samples: Dictionary of posterior samples.
    :param list compartments: A list of strings of compartment names.
    :param int duration:
    :param int population:
    """

    def __init__(self, compartments, duration, population):
        super().__init__()

        assert isinstance(duration, int)
        assert duration >= 1
        self.duration = duration

        assert isinstance(population, int)
        assert population >= 2
        self.population = population

        compartments = tuple(compartments)
        assert all(isinstance(name, str) for name in compartments)
        assert len(compartments) == len(set(compartments))
        self.compartments = compartments

        # Inference state.
        self.samples = {}

    # Overridable attributes and methods ########################################

    max_plate_nesting = 0
    series = ()
    full_mass = False

    @abstractmethod
    def heuristic(self):
        """
        """
        raise NotImplementedError

    def global_model(self):
        """
        """
        return None

    # TODO Allow stochastic initialization.
    @abstractmethod
    def initialize(self, params):
        """
        """
        raise NotImplementedError

    @abstractmethod
    def transition_fwd(self, params, state, t):
        """
        """
        raise NotImplementedError

    @abstractmethod
    def transition_bwd(self, params, prev, curr):
        """
        """
        raise NotImplementedError

    # Inference interface ########################################

    @torch.no_grad()
    def generate(self, fixed={}):
        """
        """
        model = self._generative_model
        model = poutine.condition(model, fixed)
        trace = poutine.trace(model).get_trace()
        samples = {name: site["value"]
                   for name, site in trace.nodes.items()
                   if site["type"] == "sample"}

        self._concat_series(samples)
        return samples

    def fit(self, **options):
        """
        """
        logger.info("Running inference...")
        self._dct = options.pop("dct", None)  # Save for .predict().

        # Heuristically initialze to feasible latents.
        init_values = self.heuristic()
        assert isinstance(init_values, dict)
        assert "auxiliary" in init_values, ".heuristic() did not define auxiliary value"
        if self._dct is not None:
            # Also initialize DCT transformed coordinates.
            x = init_values["auxiliary"]
            x = biject_to(constraints.interval(-0.5, self.population + 0.5)).inv(x)
            x = DiscreteCosineTransform(smooth=self._dct)(x)
            init_values["auxiliary_dct"] = x

        # Configure a kernel.
        max_tree_depth = options.pop("max_tree_depth", 5)
        full_mass = options.pop("full_mass", self.full_mass)
        model = self._vectorized_model
        if self._dct is not None:
            rep = DiscreteCosineReparam(smooth=self._dct)
            model = poutine.reparam(model, {"auxiliary": rep})
        kernel = NUTS(model,
                      full_mass=full_mass,
                      init_strategy=init_to_value(values=init_values),
                      max_tree_depth=max_tree_depth)

        # Run mcmc.
        mcmc = MCMC(kernel, **options)
        mcmc.run()
        self.samples = mcmc.get_samples()
        return mcmc  # E.g. so user can run mcmc.summary().

    @torch.no_grad()
    def predict(self, forecast=0):
        """
        """
        if not self.samples:
            raise RuntimeError("Missing samples, try running .fit() first")
        samples = self.samples
        num_samples = len(next(iter(samples.values())))
        particle_plate = pyro.plate("particles", num_samples,
                                    dim=-1 - self.max_plate_nesting)

        # Sample discrete auxiliary variables conditioned on the continuous
        # variables sampled in vectorized_model. This samples only time steps
        # [0:duration]. Here infer_discrete runs a forward-filter
        # backward-sample algorithm.
        logger.info("Predicting latent variables for {} time steps..."
                    .format(self.duration))
        model = self._sequential_model
        model = poutine.condition(model, samples)
        model = particle_plate(model)
        if self._dct is not None:
            # Apply the same reparameterizer as during inference.
            rep = DiscreteCosineReparam(smooth=self._dct)
            model = poutine.reparam(model, {"auxiliary": rep})
        model = infer_discrete(model, first_available_dim=-2 - self.max_plate_nesting)
        trace = poutine.trace(model).get_trace()
        samples = {name: site["value"]
                   for name, site in trace.nodes.items()
                   if site["type"] == "sample"}

        # Optionally forecast with the forward _generative_model. This samples
        # time steps [duration:duration+forecast].
        if forecast:
            logger.info("Forecasting {} steps ahead...".format(forecast))
            model = self._generative_model
            model = poutine.condition(model, samples)
            model = particle_plate(model)
            trace = poutine.trace(model).get_trace(forecast)
            samples = {name: site["value"]
                       for name, site in trace.nodes.items()
                       if site["type"] == "sample"}

        self._concat_series(samples, forecast)
        return samples

    # Internal helpers ########################################

    def _concat_series(self, samples, forecast=0):
        """
        Concatenate sequential time series into tensors, in-place.

        :param dict samples: A dictionary of samples.
        """
        for name in self.compartments + self.series:
            pattern = name + "_[0-9]+"
            series = [value
                      for name_t, value in samples.items()
                      if re.match(pattern, name_t)]
            if series:
                assert len(series) == self.duration + forecast
                series[0] = series[0].expand(series[1].shape)
                samples[name] = torch.stack(series, dim=-1)

    def _generative_model(self, forecast=0):
        """
        Forward generative model used for simulation and forecasting.
        """
        # Sample global parameters.
        params = self.global_model()

        # Sample initial values.
        state = self.initialize(params)
        state = {i: torch.tensor(float(value)) for i, value in state.items()}

        # Sequentially transition.
        for t in range(self.duration + forecast):
            self.transition_fwd(params, state, t)
            for name in self.compartments:
                pyro.deterministic("{}_{}".format(name, t), state[name])

    def _sequential_model(self):
        """
        Sequential model used to sample latents in the interval [0:duration].
        """
        # Sample global parameters.
        params = self.global_model()

        # Sample the continuous reparameterizing variable.
        auxiliary = pyro.sample("auxiliary",
                                dist.Uniform(-0.5, self.population + 0.5)
                                    .mask(False)
                                    .expand([len(self.compartments), self.duration])
                                    .to_event(2))

        # Sequentially transition.
        curr = self.initialize(params)
        for t in poutine.markov(range(self.duration)):
            aux_t = auxiliary[..., t]
            prev = curr
            curr = {name: quantize("{}_{}".format(name, t), aux,
                                   min=0, max=self.population)
                    for name, aux in zip(self.compartments, aux_t.unbind(-1))}
            logp = self.transition_bwd(params, prev, curr, t)
            pyro.factor("transition_{}".format(t), logp)

    def _vectorized_model(self):
        """
        Vectorized model used for inference.
        """
        # Sample global parameters.
        params = self.global_model()

        # Sample the continuous reparameterizing variable.
        auxiliary = pyro.sample("auxiliary",
                                dist.Uniform(-0.5, self.population + 0.5)
                                    .mask(False)
                                    .expand([len(self.compartments), self.duration])
                                    .to_event(2))

        # Manually enumerate.
        curr, logp = quantize_enumerate(auxiliary, min=0, max=self.population)
        curr = dict(zip(self.compartments, curr))
        logp = dict(zip(self.compartments, logp))

        # Truncate final value from the right then pad initial value onto the left.
        init = self.initialize(params)
        prev = {}
        for name in self.compartments:
            if not isinstance(init[name], int):
                raise NotImplementedError("TODO use torch.cat()")
            prev[name] = pad(curr[name][:-1], (0, 0, 1, 0), value=init[name])

        # Reshape to support broadcasting, similar to EnumMessenger.
        C = len(self.compartments)
        T = self.duration
        Q = 4  # Number of quantization points.

        def enum_shape(position):
            shape = [T] + [1] * (2 * C)
            shape[1 + position] = Q
            return torch.Size(shape)

        for e, name in enumerate(self.compartments):
            prev[name] = prev[name].reshape(enum_shape(e))
            curr[name] = curr[name].reshape(enum_shape(C + e))
            logp[name] = logp[name].reshape(enum_shape(C + e))
        t = (Ellipsis,) + (None,) * (2 * C)  # Used to unsqueeze data tensors.

        # Manually perform variable elimination.
        logp = sum(logp.values())
        logp = logp + self.transition_bwd(params, prev, curr, t)
        logp = logp.reshape(T, Q ** C, Q ** C)
        logp = pyro.distributions.hmm._sequential_logmatmulexp(logp)
        logp = logp.reshape(-1).logsumexp(0)
        warn_if_nan(logp)
        pyro.factor("transition", logp)