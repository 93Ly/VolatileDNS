"""When does the interval-censored estimator (Eqn. 4.5) matter?

    python -m testbed.estimator_study        # writes results/estimator_study.json and results/fig_estimator_bias.png

A record changes at random (Poisson) with a true mean gap G.  The proxy refreshes it every D seconds and sees a change only if
the address set differs from the previous refresh.  For each ratio D/G we measure, over many random histories, the mean ratio
estimate / truth of the simple estimator (Eqn. 4.1) and of the improved one (Eqn. 4.5), with a negligible starting guess so
that the estimators themselves are compared.
"""
import json
import os
import random
import statistics as st

from volatiledns.history import HistoryTracker

RATIOS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0]
GAP = 100.0
STEPS = 400
SEEDS = 300


def one(ratio, seed):
    rng = random.Random(seed)
    true_rate = 1.0 / GAP
    dt = ratio * GAP
    trackers = {k: HistoryTracker(prior_changes=0.01, prior_seconds=1.0, estimator=k) for k in ("counted", "censored")}
    t_change, ip, t = rng.expovariate(true_rate), 0, 0.0
    for tr in trackers.values():
        tr.observe("n", ["a0"], 0.0)
    for _ in range(STEPS):
        t += dt
        while t_change <= t:
            ip += 1
            t_change += rng.expovariate(true_rate)
        for tr in trackers.values():
            tr.observe("n", [f"a{ip}"], t)
    return {k: tr.rate("n", t) / true_rate for k, tr in trackers.items()}


def main():
    res = {}
    for r in RATIOS:
        runs = [one(r, s) for s in range(SEEDS)]
        res[str(r)] = {k: dict(mean=st.mean(x[k] for x in runs), sd=st.stdev(x[k] for x in runs)) for k in ("counted", "censored")}
        print(f"D/G={r:5.2f}  counted {res[str(r)]['counted']['mean']:.3f}  censored {res[str(r)]['censored']['mean']:.3f}", flush=True)
    os.makedirs("results", exist_ok=True)
    json.dump(res, open("results/estimator_study.json", "w"), indent=1)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams.update({"font.family": "serif", "font.size": 10})
        fig, ax = plt.subplots(figsize=(6.2, 3.9))
        xs = RATIOS
        ax.plot(xs, [res[str(r)]["counted"]["mean"] for r in xs], "o-", color="black", label="counted, Eqn. 4.1")
        ax.plot(xs, [res[str(r)]["censored"]["mean"] for r in xs], "s--", color="#555555", label="interval-censored, Eqn. 4.5")
        ax.axhline(1.0, color="grey", lw=0.8)
        ax.set_xscale("log")
        ax.set_xlabel("Refresh interval / mean time between changes")
        ax.set_ylabel("Estimated rate / true rate")
        ax.grid(alpha=0.3, which="both")
        ax.legend()
        fig.tight_layout()
        fig.savefig("results/fig_estimator_bias.png", dpi=200)
    except ImportError:
        pass


if __name__ == "__main__":
    main()
