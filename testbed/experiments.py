"""Run every experiment of Chapter 5 and write CSV/JSON results + figures into ../results.

    python -m testbed.experiments            # full run (about 10-15 minutes)
    python -m testbed.experiments --quick    # 12 seeds, for a smoke test
"""
import argparse
import csv
import json
import math
import os
import random
import statistics as st
import sys
import time
from collections import defaultdict

from volatiledns.config import Config
from volatiledns.history import stale_limit_volatile
from testbed.simulate import (SPECS, NameSpec, World, outage_start, run_outage, warm_engine)

WARM = 12 * 3600.0
DURATIONS = [120, 300, 600, 1200]          # outage lengths: 2, 5, 10, 20 minutes
CAP = 1200.0

DEFAULTS = {
    "none": Config(policy="none", s_cap=CAP),
    "fixed": Config(policy="fixed", s_cap=CAP, s_fix=CAP),
    "ttl": Config(policy="ttl", s_cap=CAP, ttl_mult=10.0),
    "volatile": Config(policy="volatile", s_cap=CAP, epsilon=0.05),
}
SWEEP = {
    "fixed": [("s_fix", v) for v in (0, 60, 120, 300, 600, 900, 1200)],
    "ttl": [("ttl_mult", v) for v in (0, 1, 2, 5, 10, 20)],
    "volatile": [("epsilon", v) for v in (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5)],
}
COUNT_KEYS = ["total", "cache_ok", "cache_wrong", "stale_ok", "stale_wrong", "servfail", "nxdomain",
              "datagrams"]


# ---------------------------------------------------------------------------
def one_seed(args):
    """Everything that needs one random seed (runs in a worker process)."""
    seed, scenario, grace, history_variants = args
    rows = []
    base_cfg = Config(s_cap=CAP)
    specs = dict(SPECS)
    if scenario == "gone":
        specs["gone.college.test."] = NameSpec(ttl=30, gap=None)
    horizon = WARM + 3000

    # learning period: depends only on the history settings, not on the stale policy
    warmed = {}
    # "gone" scenario: the name is deleted 200 s before the end of the learning period, so the
    # proxy has already received (and cached) the authoritative NXDOMAIN when the outage starts
    deleted0 = {"gone.college.test.": WARM - 200.0} if scenario == "gone" else None
    for hv_name, hv in history_variants.items():
        world, eng = warm_engine(seed, specs, WARM, base_cfg.with_(**hv), horizon=horizon, grace=grace,
                                 deleted=deleted0)
        warmed[hv_name] = (world, eng)

    # estimator snapshot at the end of the learning period
    est = []
    world0, eng0 = warmed["default"]
    for name in SPECS:
        key = f"{name}/1"
        rate = eng0.history.rate(key, WARM)
        h = eng0.history.names[key]
        rate_c = warmed["censored"][1].history.rate(key, WARM) if "censored" in warmed else rate
        est.append(dict(seed=seed, name=name, rate=rate, rate_c=rate_c, counted=h.changes,
                        true=world0.true_changes(name, 0, WARM),
                        ttl=specs[name].ttl,
                        gap=specs[name].gap or 0))

    for D in DURATIONS:
        start = outage_start(seed, WARM)
        forced = None
        deleted = deleted0
        if scenario == "forced":
            rng = random.Random(seed * 31 + D)
            forced = {n: [start + rng.uniform(0.2, 0.8) * D] for n, sp in SPECS.items() if not sp.rotating}
        world_d = World(seed, specs, horizon, grace, forced, deleted) if (forced or deleted) else None
        for label, cfg, hv in iter_configs(scenario, history_variants):
            world, eng = warmed[hv]
            if world_d is not None:
                world = world_d
            out = run_outage(seed, eng, world, cfg, WARM, D, poll=5.0)
            for name, o in out.items():
                rows.append(dict(seed=seed, scenario=scenario, grace=grace, duration=D, config=label,
                                 name=name, recovery=o["recovery"], **{k: o[k] for k in COUNT_KEYS}))
    return rows, est


def iter_configs(scenario, history_variants):
    """(label, Config, history-variant-name) triples to evaluate in a scenario."""
    for label, cfg in DEFAULTS.items():
        yield label, cfg, "default"
    if scenario == "natural":
        for pol, params in SWEEP.items():
            for k, v in params:
                yield f"{pol}:{k}={v}", DEFAULTS[pol].with_(**{k: v}), "default"
        yield "volatile:strict", DEFAULTS["volatile"].with_(count_rotation=True), "strict"
        yield "volatile:censored", DEFAULTS["volatile"].with_(estimator="censored"), "censored"
        for beta in (600.0, 14400.0):
            yield f"volatile:beta={int(beta)}", DEFAULTS["volatile"].with_(prior_seconds=beta), f"beta{int(beta)}"


HISTORY_VARIANTS = {
    "default": {},
    "strict": {"count_rotation": True},
    "beta600": {"prior_seconds": 600.0},
    "beta14400": {"prior_seconds": 14400.0},
    "censored": {"estimator": "censored"},
}


# ---------------------------------------------------------------------------
def ci95(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    return 1.96 * st.stdev(xs) / math.sqrt(len(xs))


def pooled(rows, keyfn, only=lambda r: True):
    """Micro-average per key, with a 95% CI from the per-seed ratios."""
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if only(r):
            by[keyfn(r)][r["seed"]].append(r)
    res = {}
    for k, seeds in by.items():
        per_seed = []
        tot = defaultdict(float)
        for s, rs in seeds.items():
            c = {x: sum(r[x] for r in rs) for x in COUNT_KEYS}
            for x in COUNT_KEYS:
                tot[x] += c[x]
            if c["total"]:
                per_seed.append(c)
        res[k] = summarise(tot, per_seed)
    return res


def summarise(tot, per_seed):
    def ratio(num, c):
        return 100.0 * sum(c[n] for n in num) / c["total"] if c["total"] else 0.0
    T = tot["total"] or 1.0
    avail = 100.0 * (tot["cache_ok"] + tot["stale_ok"]) / T
    wrong = 100.0 * (tot["cache_wrong"] + tot["stale_wrong"]) / T
    fail = 100.0 * tot["servfail"] / T
    stale_n = tot["stale_ok"] + tot["stale_wrong"]
    return dict(
        queries=int(tot["total"]),
        avail=avail, wrong=wrong, servfail=fail,
        avail_ci=ci95([ratio(["cache_ok", "stale_ok"], c) for c in per_seed]),
        wrong_ci=ci95([ratio(["cache_wrong", "stale_wrong"], c) for c in per_seed]),
        stale_served=int(stale_n),
        stale_wrong_pct=100.0 * tot["stale_wrong"] / stale_n if stale_n else 0.0,
        upstream_per_query=tot["datagrams"] / T,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="results")
    ap.add_argument("--figures-only", action="store_true")
    a = ap.parse_args()
    if a.figures_only:
        make_figures(json.load(open(os.path.join(a.out, "summary.json"))), a.out)
        return 0
    seeds = list(range(12 if a.quick else 200))
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    jobs = []
    for s in seeds:
        jobs.append((s, "natural", 0.0, HISTORY_VARIANTS))
        jobs.append((s, "forced", 0.0, {"default": {}}))
        jobs.append((s, "forced", 120.0, {"default": {}}))
        jobs.append((s, "gone", 0.0, {"default": {}}))
    all_rows, est = [], []
    for i, job in enumerate(jobs):                      # sequential: ~1 s per seed
        rows, e = one_seed(job)
        all_rows += rows
        est += e
        if i % 100 == 0:
            print(f"  {i}/{len(jobs)} jobs, {time.time() - t0:.0f}s", flush=True)
    print(f"simulated {len(all_rows)} result rows in {time.time() - t0:.0f}s", flush=True)

    with open(os.path.join(a.out, "raw_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)

    summary = {"seeds": len(seeds), "warm_seconds": WARM, "durations": DURATIONS, "cap": CAP}

    def sel(scn, grace=0.0):
        return lambda r: r["scenario"] == scn and r["grace"] == grace

    nat = [r for r in all_rows if r["scenario"] == "natural"]
    summary["natural_by_name_600"] = {
        f"{k[0]}|{k[1]}": v for k, v in pooled(nat, lambda r: (r["config"], r["name"]),
                                                 lambda r: r["duration"] == 600 and r["config"] in DEFAULTS).items()}
    summary["natural_by_duration"] = {
        f"{k[0]}|{k[1]}": v for k, v in pooled(nat, lambda r: (r["config"], r["duration"]),
                                                 lambda r: r["config"] in DEFAULTS).items()}
    summary["natural_pooled"] = {k: v for k, v in pooled(nat, lambda r: r["config"]).items()}
    f0 = [r for r in all_rows if r["scenario"] == "forced" and r["grace"] == 0.0]
    f120 = [r for r in all_rows if r["scenario"] == "forced" and r["grace"] == 120.0]
    for lab, rr in (("forced_g0", f0), ("forced_g120", f120)):
        summary[lab + "_by_name_600"] = {
            f"{k[0]}|{k[1]}": v for k, v in pooled(rr, lambda r: (r["config"], r["name"]),
                                                     lambda r: r["duration"] == 600).items()}
        summary[lab + "_pooled"] = pooled(rr, lambda r: r["config"])
    summary["forced_g0_by_duration"] = {f"{k[0]}|{k[1]}": v for k, v in
                                        pooled(f0, lambda r: (r["config"], r["duration"])).items()}

    gone = [r for r in all_rows if r["scenario"] == "gone" and r["name"] == "gone.college.test."]
    summary["gone"] = {c: dict(queries=sum(r["total"] for r in gone if r["config"] == c),
                               stale=sum(r["stale_ok"] + r["stale_wrong"] for r in gone if r["config"] == c),
                               nxdomain=sum(r["nxdomain"] for r in gone if r["config"] == c),
                               servfail=sum(r["servfail"] for r in gone if r["config"] == c))
                       for c in DEFAULTS}

    rec = defaultdict(list)
    for r in nat:
        if r["config"] in DEFAULTS and r["recovery"] is not None:
            rec[r["config"]].append(r["recovery"])
    summary["recovery"] = {c: dict(mean=st.mean(v), p95=sorted(v)[int(0.95 * len(v))], max=max(v), n=len(v))
                           for c, v in rec.items()}

    # estimator quality at the end of the learning period
    e_by = defaultdict(list)
    for e in est:
        e_by[e["name"]].append(e)
    summary["estimator"] = {}
    for n, es in e_by.items():
        rate = st.mean(x["rate"] for x in es)
        ttl = es[0]["ttl"]
        rate_c = st.mean(x["rate_c"] for x in es)
        gap = es[0]["gap"]
        summary["estimator"][n] = dict(
            ttl=ttl, true_gap=es[0]["gap"], mean_rate=rate, mean_rate_censored=rate_c, est_gap_censored=1 / rate_c,
            rel_err_counted=st.mean(abs(x["rate"] * gap - 1) for x in es) if gap else None,
            rel_err_censored=st.mean(abs(x["rate_c"] * gap - 1) for x in es) if gap else None,
            est_gap=1 / rate, counted=st.mean(x["counted"] for x in es),
            true_changes=st.mean(x["true"] for x in es),
            limit_volatile=st.mean(min(CAP, stale_limit_volatile(x["rate"], ttl, 0.05)) for x in es),
            limit_ttl=min(CAP, 10 * ttl), limit_fixed=CAP)
        summary["estimator"][n]["limit_volatile"] = max(-1.0, summary["estimator"][n]["limit_volatile"])

    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print("wrote summary.json", flush=True)
    try:
        make_figures(summary, a.out)
    except ImportError:
        print("matplotlib missing: run  python -m testbed.experiments --figures-only", flush=True)
    print(f"done in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
def make_figures(S, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "serif", "font.size": 10})
    pol_color = {"none": "#7f7f7f", "fixed": "#d95f02", "ttl": "#1b9e77", "volatile": "#1f4e9c"}
    short = lambda n: n.replace(".college.test.", "")
    names = [short(n) for n in SPECS]

    # Fig 5.1 reachability / wrong-answer trade-off
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    P = S["natural_pooled"]
    for pol in ("fixed", "ttl", "volatile"):
        pts = [(P[f"{pol}:{k}={v}"]["wrong"], P[f"{pol}:{k}={v}"]["avail"], v) for k, v in
               [(k, v) for k, v in SWEEP[pol]]]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], "-o", ms=4, color=pol_color[pol], label=pol)
        d = P[pol]
        ax.plot(d["wrong"], d["avail"], "o", ms=11, mfc="none", mec=pol_color[pol], mew=2)
    ax.plot(P["none"]["wrong"], P["none"]["avail"], "s", color=pol_color["none"], label="none")
    ax.set_xlabel("Answers pointing to a dead address (% of client queries during outage)")
    ax.set_ylabel("Queries answered with a working address (%)")
    ax.legend(title="policy (ring = default setting)")
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_tradeoff.png"), dpi=200)
    plt.close(fig)

    # Fig 5.2 outcomes per name (natural world, 10-minute outage)
    for key, fname, title in (("natural_by_name_600", "fig_by_name_natural.png", "Natural changes"),
                              ("forced_g0_by_name_600", "fig_by_name_forced.png", "A service moves during the outage")):
        fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.6), sharey=True)
        W = 0.2
        for ax, (metric, lab) in zip(axes, (("avail", "Working answer (%)"), ("wrong", "Dead address (%)"),
                                            ("servfail", "SERVFAIL (%)"))):
            for i, pol in enumerate(DEFAULTS):
                vals = [S[key][f"{pol}|{n}"][metric] for n in SPECS]
                ax.bar([x + (i - 1.5) * W for x in range(len(names))], vals, W, color=pol_color[pol], label=pol)
            ax.set_xticks(range(len(names)))
            ax.set_xticklabels(names, rotation=35, ha="right")
            ax.set_title(lab)
            ax.set_ylim(0, 118)
            ax.grid(axis="y", alpha=.3)
        axes[0].set_ylabel("% of client queries")
        axes[2].legend(fontsize=8, loc="upper right", ncol=2)
        fig.suptitle(title + " - 10 min outage", y=1.0)
        fig.tight_layout()
        fig.savefig(os.path.join(out, fname), dpi=200)
        plt.close(fig)

    # Fig 5.3 stale limit each policy assigns to each name
    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    E = S["estimator"]
    for i, (pol, key) in enumerate((("fixed", "limit_fixed"), ("ttl", "limit_ttl"), ("volatile", "limit_volatile"))):
        ax.bar([x + (i - 1) * 0.27 for x in range(len(names))], [max(0, E[n][key]) / 60 for n in SPECS], 0.27,
               color=pol_color[pol], label=pol)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=25, ha="right")
    ax.set_ylabel("Stale limit (minutes)")
    ax.set_ylim(0, 25)
    ax.legend(ncol=3, loc="upper center")
    ax.grid(axis="y", alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_limits.png"), dpi=200)
    plt.close(fig)

    # Fig 5.4 effect of outage length
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
    D = S["natural_by_duration"]
    for pol in DEFAULTS:
        axes[0].plot([d / 60 for d in DURATIONS], [D[f"{pol}|{d}"]["avail"] for d in DURATIONS], "-o", color=pol_color[pol], label=pol)
        axes[1].plot([d / 60 for d in DURATIONS], [D[f"{pol}|{d}"]["wrong"] for d in DURATIONS], "-o", color=pol_color[pol], label=pol)
    axes[0].set_ylabel("Working answers (%)")
    axes[1].set_ylabel("Dead-address answers (%)")
    for ax in axes:
        ax.set_xlabel("Outage length (minutes)")
        ax.grid(alpha=.3)
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_duration.png"), dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    sys.exit(main())
