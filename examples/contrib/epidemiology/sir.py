# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

# This script aims to replicate the behavior of examples/sir_hmc.py but using
# the high-level components of pyro.contrib.epidemiology. Command line
# arguments and results should be similar.

import argparse
import logging
import math

import torch

import pyro
from pyro.contrib.epidemiology import OverdispersedSEIRModel, OverdispersedSIRModel, SimpleSEIRModel, SimpleSIRModel

logging.basicConfig(format='%(message)s', level=logging.INFO)


def Model(args, data):
    """Dispatch between different model classes."""
    if args.incubation_time > 0:
        assert args.incubation_time > 1
        if args.concentration == math.inf:
            return SimpleSEIRModel(args.population, args.incubation_time,
                                   args.recovery_time, data)
        else:
            return OverdispersedSEIRModel(args.population, args.incubation_time,
                                          args.recovery_time, data)
    else:
        if args.concentration == math.inf:
            return SimpleSIRModel(args.population, args.recovery_time, data)
        else:
            return OverdispersedSIRModel(args.population, args.recovery_time, data)


def generate_data(args):
    extended_data = [None] * (args.duration + args.forecast)
    model = Model(args, extended_data)
    logging.info("Simulating from a {}".format(type(model).__name__))
    for attempt in range(100):
        samples = model.generate({"R0": args.basic_reproduction_number,
                                  "rho": args.response_rate,
                                  "k": args.concentration})
        obs = samples["obs"][:args.duration]
        new_I = samples.get("S2I", samples.get("E2I"))

        obs_sum = int(obs.sum())
        new_I_sum = int(new_I[:args.duration].sum())
        if obs_sum >= args.min_observations:
            logging.info("Observed {:d}/{:d} infections:\n{}".format(
                obs_sum, new_I_sum, " ".join(str(int(x)) for x in obs)))
            return {"new_I": new_I, "obs": obs}

    raise ValueError("Failed to generate {} observations. Try increasing "
                     "--population or decreasing --min-observations"
                     .format(args.min_observations))


def infer(args, model):
    energies = []

    def hook_fn(kernel, *unused):
        e = float(kernel._potential_energy_last)
        energies.append(e)
        if args.verbose:
            logging.info("potential = {:0.6g}".format(e))

    mcmc = model.fit(heuristic_num_particles=args.num_particles,
                     warmup_steps=args.warmup_steps,
                     num_samples=args.num_samples,
                     max_tree_depth=args.max_tree_depth,
                     num_quant_bins=args.num_bins,
                     dct=args.dct,
                     hook_fn=hook_fn)

    mcmc.summary()
    if args.plot:
        import matplotlib.pyplot as plt
        plt.figure(figsize=(6, 3))
        plt.plot(energies)
        plt.xlabel("MCMC step")
        plt.ylabel("potential energy")
        plt.title("MCMC energy trace")
        plt.tight_layout()

    return model.samples


def evaluate(args, samples):
    # Print estimated values.
    names = {"basic_reproduction_number": "R0",
             "response_rate": "rho"}
    if args.concentration < math.inf:
        names["concentration"] = "k"
    for name, key in names.items():
        mean = samples[key].mean().item()
        std = samples[key].std().item()
        logging.info("{}: truth = {:0.3g}, estimate = {:0.3g} \u00B1 {:0.3g}"
                     .format(key, getattr(args, name), mean, std))

    # Optionally plot histograms and pairwise correlations.
    if args.plot:
        import matplotlib.pyplot as plt
        import seaborn as sns

        fig, axes = plt.subplots(len(names), 1, figsize=(5, 2.5 * len(names)))
        axes[0].set_title("Posterior parameter estimates")
        for ax, (name, key) in zip(axes, names.items()):
            truth = getattr(args, name)
            sns.distplot(samples[key], ax=ax, label="posterior")
            ax.axvline(truth, color="k", label="truth")
            ax.set_xlabel(key + " = " + name.replace("_", " "))
            ax.set_yticks(())
            ax.legend(loc="best")
        plt.tight_layout()

        covariates = [(name, samples[name]) for name in names.values()]
        for i, aux in enumerate(samples["auxiliary"].unbind(-2)):
            covariates.append(("aux[{},0]".format(i), aux[:, 0]))
            covariates.append(("aux[{},-1]".format(i), aux[:, -1]))
        N = len(covariates)
        fig, axes = plt.subplots(N, N, figsize=(8, 8), sharex="col", sharey="row")
        for i in range(N):
            axes[i][0].set_ylabel(covariates[i][0])
            axes[0][i].set_xlabel(covariates[i][0])
            axes[0][i].xaxis.set_label_position("top")
            for j in range(N):
                ax = axes[i][j]
                ax.set_xticks(())
                ax.set_yticks(())
                ax.scatter(covariates[j][1], -covariates[i][1],
                           lw=0, color="darkblue", alpha=0.3)
        plt.tight_layout()
        plt.subplots_adjust(wspace=0, hspace=0)


def predict(args, model, truth):
    samples = model.predict(forecast=args.forecast)

    obs = model.data

    new_I = samples.get("S2I", samples.get("E2I"))
    median = new_I.median(dim=0).values
    logging.info("Median prediction of new infections (starting on day 0):\n{}"
                 .format(" ".join(map(str, map(int, median)))))

    # Optionally plot the latent and forecasted series of new infections.
    if args.plot:
        import matplotlib.pyplot as plt
        plt.figure()
        time = torch.arange(args.duration + args.forecast)
        p05 = new_I.kthvalue(int(round(0.5 + 0.05 * args.num_samples)), dim=0).values
        p95 = new_I.kthvalue(int(round(0.5 + 0.95 * args.num_samples)), dim=0).values
        plt.fill_between(time, p05, p95, color="red", alpha=0.3, label="90% CI")
        plt.plot(time, median, "r-", label="median")
        plt.plot(time[:args.duration], obs, "k.", label="observed")
        if truth is not None:
            plt.plot(time, truth, "k--", label="truth")
        plt.axvline(args.duration - 0.5, color="gray", lw=1)
        plt.xlim(0, len(time) - 1)
        plt.ylim(0, None)
        plt.xlabel("day after first infection")
        plt.ylabel("new infections per day")
        plt.title("New infections in population of {}".format(args.population))
        plt.legend(loc="upper left")
        plt.tight_layout()


def main(args):
    pyro.enable_validation(__debug__)
    pyro.set_rng_seed(args.rng_seed)

    # Generate data.
    dataset = generate_data(args)
    obs = dataset["obs"]

    # Run inference.
    model = Model(args, obs)
    samples = infer(args, model)

    # Evaluate fit.
    evaluate(args, samples)

    # Predict latent time series.
    if args.forecast:
        predict(args, model, truth=dataset["new_I"])


if __name__ == "__main__":
    assert pyro.__version__.startswith('1.3.1')
    parser = argparse.ArgumentParser(
        description="Compartmental epidemiology modeling using HMC")
    parser.add_argument("-p", "--population", default=1000, type=int)
    parser.add_argument("-m", "--min-observations", default=3, type=int)
    parser.add_argument("-d", "--duration", default=20, type=int)
    parser.add_argument("-f", "--forecast", default=10, type=int)
    parser.add_argument("-R0", "--basic-reproduction-number", default=1.5, type=float)
    parser.add_argument("-tau", "--recovery-time", default=7.0, type=float)
    parser.add_argument("-e", "--incubation-time", default=0.0, type=float,
                        help="If zero, use SIR model; if > 1 use SEIR model.")
    parser.add_argument("-k", "--concentration", default=math.inf, type=float,
                        help="If finite, use a superspreader model.")
    parser.add_argument("-rho", "--response-rate", default=0.5, type=float)
    parser.add_argument("--dct", type=float,
                        help="smoothing for discrete cosine reparameterizer")
    parser.add_argument("-n", "--num-samples", default=200, type=int)
    parser.add_argument("-np", "--num-particles", default=1024, type=int)
    parser.add_argument("-w", "--warmup-steps", default=100, type=int)
    parser.add_argument("-t", "--max-tree-depth", default=5, type=int)
    parser.add_argument("-r", "--rng-seed", default=0, type=int)
    parser.add_argument("-nb", "--num-bins", default=4, type=int)
    parser.add_argument("--double", action="store_true")
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()

    if args.double:
        if args.cuda:
            torch.set_default_tensor_type(torch.cuda.DoubleTensor)
        else:
            torch.set_default_tensor_type(torch.DoubleTensor)
    elif args.cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    main(args)

    if args.plot:
        import matplotlib.pyplot as plt
        plt.show()
