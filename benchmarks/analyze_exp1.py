#!/usr/bin/env python3
"""analyze_exp1.py -- turns results/exp1_baseline/ into a summary table, a CSV, and plots."""
import argparse
import csv
import glob
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))


def load_runs(root):
    """One flat dictionary per run folder that contains a summary.json."""
    runs = []
    for path in sorted(glob.glob(os.path.join(root, "*", "run*", "summary.json"))):
        run_dir = os.path.dirname(path)
        condition = os.path.basename(os.path.dirname(run_dir))      # e.g. "8gb_always"
        size, thp = condition.rsplit("_", 1)
        with open(path) as f:
            s = json.load(f)
        k = s.get("kernel_deltas", {})
        misses = s.get("perf", {}).get("dTLB-load-misses")
        accesses = s.get("access_total")
        runs.append({
            "size": size,
            "thp": thp,
            "rep": int(os.path.basename(run_dir)[3:]),
            "dir": run_dir,
            "valid": not s.get("validity_problems"),
            "alloc_total_s": s.get("alloc_total_s"),
            "alloc_chunk_median_ms": s.get("alloc_chunk_median_ms"),
            "alloc_chunk_max_ms": s.get("alloc_chunk_max_ms"),
            "alloc_sys_s": s.get("alloc_sys_s"),
            "access_mean_per_s": s.get("access_mean_per_s"),
            "access_sys_s": s.get("access_sys_s"),
            "dtlb_miss_rate": s.get("dtlb_miss_rate"),
            "dtlb_misses_per_access": (misses / accesses) if misses is not None and accesses else None,
            "thp_fault_alloc": k.get("thp_fault_alloc"),
            "thp_fault_fallback": k.get("thp_fault_fallback"),
            "alloc_user_s": s.get("alloc_user_s"),
            "alloc_faults": s.get("alloc_faults"),
            "huge_mb_after_alloc": s.get("after_alloc_anon_huge_mb"),
        })
    return runs

# (key in the run record, label shown in the table)
METRICS = [
    ("alloc_total_s", "allocation time (s)", "time to touch every page once", "lower"),
    ("alloc_chunk_median_ms", "median 2 MB chunk (ms)", "typical time per 2 MB piece", "lower"),
    ("alloc_chunk_max_ms", "slowest 2 MB chunk (ms)", "worst stall on any 2 MB piece", "lower"),
    ("alloc_sys_s", "allocation system CPU (s)", "kernel time during allocation", "lower"),
    ("alloc_user_s", "allocation user CPU (s)", "program's own time during allocation", "lower"),
    ("alloc_faults", "allocation page faults", "times the kernel stepped in to hand out memory", "lower"),
    ("access_mean_per_s", "accesses per second", "random accesses completed each second", "higher"),
    ("access_sys_s", "access-step system CPU (s)", "kernel time during the access step", None),
    ("dtlb_miss_rate", "translation miss rate", "share of loads missing the translation cache", "lower"),
    ("dtlb_misses_per_access", "translation misses per access", "misses divided by array accesses", "lower"),
    ("huge_mb_after_alloc", "huge-page memory (MB)", "validity check: memory backed by 2 MB pages", None),
    ("thp_fault_alloc", "huge pages at fault time", "validity check: faults that got a 2 MB page", None),
]


def median_range(values):
    """(median, smallest, largest) of the values that exist, or None if none exist."""
    vals = [v for v in values if v is not None]
    return (statistics.median(vals), min(vals), max(vals)) if vals else None


def summarize(runs):
    """Group valid runs by (size, thp setting) and summarize every metric."""
    groups = {}
    for r in runs:
        if r["valid"]:
            groups.setdefault((r["size"], r["thp"]), []).append(r)
    summary = {}
    for key, rs in groups.items():
        summary[key] = {"n": len(rs)}
        for metric, *_ in METRICS:
            summary[key][metric] = median_range([r[metric] for r in rs])
    return summary


def comparisons(summary, size):
    """THP-on versus THP-off for one size. In every entry, above 1 means THP is better."""
    on, off = summary.get((size, "always")), summary.get((size, "never"))
    if not on or not off:
        return {}
    def med(group, metric):
        return group[metric][0] if group[metric] else None
    pairs = {
        "access speedup (accesses per second, on / off)": (med(on, "access_mean_per_s"), med(off, "access_mean_per_s")),
        "allocation speedup (time, off / on)": (med(off, "alloc_total_s"), med(on, "alloc_total_s")),
        "slowest-chunk speedup (time, off / on)": (med(off, "alloc_chunk_max_ms"), med(on, "alloc_chunk_max_ms")),
    }
    return {name: a / b for name, (a, b) in pairs.items() if a and b}


def improvement(on, off, better):
    """How much better THP on is. Returns (factor, percent), or (None, None) if not comparable."""
    if better is None or not on or not off:
        return None, None
    if better == "higher":
        return on / off, (on / off - 1) * 100       # e.g. 1.49x, 48.7% more
    return off / on, (1 - on / off) * 100           # e.g. 2.84x, 64.8% less


def print_and_save(summary, runs, outdir):
    sizes = sorted({size for size, _ in summary})
    bad = [r for r in runs if not r["valid"]]
    print("runs found: %d, excluded as invalid: %d" % (len(runs), len(bad)))
    for r in bad:
        print("  excluded:", r["dir"])

    with open(os.path.join(outdir, "summary_long.csv"), "w", newline="") as f_long, \
         open(os.path.join(outdir, "summary_side_by_side.csv"), "w", newline="") as f_wide:
        long_w, wide_w = csv.writer(f_long), csv.writer(f_wide)
        long_w.writerow(["size", "thp", "metric", "median", "min", "max", "n_runs"])
        wide_w.writerow(["size", "metric", "what_it_measures", "thp_on_median", "thp_off_median",
                         "factor_thp_on_better", "percent_thp_on_better"])
        for size in sizes:
            on, off = summary.get((size, "always")), summary.get((size, "never"))
            print("\n=== %s ===" % size)
            print("%-32s %11s %11s %10s %10s" % ("metric", "THP on", "THP off", "factor", "percent"))
            for metric, label, what, better in METRICS:
                for thp, g in (("always", on), ("never", off)):
                    if g and g[metric]:
                        long_w.writerow([size, thp, metric, *g[metric], g["n"]])
                if not (on and off and on[metric] and off[metric]):
                    continue
                a, b = on[metric][0], off[metric][0]
                factor, percent = improvement(a, b, better)
                f_txt = "%.2fx" % factor if factor else "-"
                p_txt = "%.1f%%" % percent if percent is not None else "-"
                print("%-32s %11.4g %11.4g %10s %10s" % (label, a, b, f_txt, p_txt))
                wide_w.writerow([size, metric, what, a, b,
                                 round(factor, 3) if factor else "",
                                 round(percent, 1) if percent is not None else ""])
                                 

COLORS = {"always": "tab:blue", "never": "tab:red"}


def plot_timeseries(runs, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")                     # draw to a file; no screen is needed on the node
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed, so plots were skipped (sudo apt install python3-matplotlib)")
        return
    for size in sorted({r["size"] for r in runs}):
        fig, ax = plt.subplots(figsize=(8, 4.5))
        labeled = set()
        for r in runs:
            if r["size"] != size:
                continue
            t, y = [], []
            with open(os.path.join(r["dir"], "memhog.csv")) as f:
                for row in csv.DictReader(f):
                    t.append(float(row["t_s"]))
                    y.append(float(row["acc_per_s"]) / 1e6)
            label = None if r["thp"] in labeled else "THP " + r["thp"]
            labeled.add(r["thp"])
            ax.plot(t, y, color=COLORS[r["thp"]], alpha=0.7, label=label)
        ax.set_xlabel("seconds into the access step")
        ax.set_ylabel("millions of accesses per second")
        ax.set_title("Experiment 1, %s array" % size)
        ax.set_ylim(bottom=0)
        ax.legend()
        fig.savefig(os.path.join(outdir, "accesses_%s.png" % size), dpi=150, bbox_inches="tight")
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(HERE, "results", "exp1_baseline"))
    a = ap.parse_args()
    runs = load_runs(a.root)
    if not runs:
        raise SystemExit("no summary.json files found under " + a.root)
    outdir = os.path.join(a.root, "analysis")
    os.makedirs(outdir, exist_ok=True)
    summary = summarize(runs)
    print_and_save(summary, runs, outdir)
    plot_timeseries(runs, outdir)
    print("\nsaved to", outdir)


if __name__ == "__main__":
    main()

    