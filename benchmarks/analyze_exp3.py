#!/usr/bin/env python3
"""analyze_exp3.py -- tables and time-series plots for results/exp3_pressure/ ."""
import argparse
import csv
import glob
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))

CONDITIONS = ["thp-off", "defrag-never", "defrag-defer", "defrag-always"]
LABELS = {"thp-off": "THP off", "defrag-never": "THP on, defrag never",
          "defrag-defer": "THP on, defrag defer", "defrag-always": "THP on, defrag always"}
COLORS = {"thp-off": "tab:gray", "defrag-never": "tab:green",
          "defrag-defer": "tab:orange", "defrag-always": "tab:red"}

# Values that memhog prints itself; every other metric is a kernel counter change over the run.
FROM_PROGRAM = {"alloc_total_s", "access_mean_per_s", "access_majflt", "access_sys_s",
                "after_alloc_rss_mb", "after_alloc_swap_mb", "end_rss_mb", "end_swap_mb",
                "after_alloc_anon_huge_mb", "end_anon_huge_mb"}
DERIVED = {"fallback_fraction", "acc_first", "acc_last", "acc_trend"}

METRICS = [
    ("alloc_total_s", "allocation time (s)"),
    ("access_mean_per_s", "accesses per second, whole access step"),
    ("acc_first", "accesses per second, first window"),
    ("acc_last", "accesses per second, last window"),
    ("acc_trend", "last window / first window"),
    ("access_majflt", "program's major faults (swap-ins it waited for)"),
    ("access_sys_s", "program's system CPU in access step (s)"),
    ("after_alloc_rss_mb", "resident memory after allocation (MB)"),
    ("after_alloc_swap_mb", "swapped-out memory after allocation (MB)"),
    ("end_rss_mb", "resident memory at the end (MB)"),
    ("end_swap_mb", "swapped-out memory at the end (MB)"),
    ("after_alloc_anon_huge_mb", "memory in huge pages after allocation (MB)"),
    ("end_anon_huge_mb", "memory in huge pages at the end (MB)"),
    ("thp_fault_alloc", "huge pages obtained at fault time"),
    ("thp_fault_fallback", "huge-page attempts that fell back to 4 KB"),
    ("thp_swpout", "huge pages swapped out whole"),
    ("thp_swpout_fallback", "huge pages split before swap-out"),
    ("fallback_fraction", "share of huge-page evictions split first"),
    ("thp_split_page", "huge pages split (any reason)"),
    ("thp_split_page_failed", "split attempts that failed"),
    ("thp_collapse_alloc", "huge pages rebuilt by khugepaged"),
    ("pswpout", "4 KB pages written to swap"),
    ("pswpin", "4 KB pages read from swap"),
    ("pgmajfault", "major page faults (whole system)"),
    ("pgscan_kswapd", "pages examined by kswapd"),
    ("pgsteal_kswapd", "pages freed by kswapd"),
    ("pgscan_direct", "pages examined by the program's own reclaim"),
    ("pgsteal_direct", "pages freed by the program's own reclaim"),
    ("allocstall", "allocation stalls (direct reclaim)"),
    ("compact_stall", "direct compaction attempts"),
    ("cpu_kswapd0_s", "kswapd CPU time (s)"),
    ("cpu_kcompactd0_s", "kcompactd CPU time (s)"),
    ("cpu_khugepaged_s", "khugepaged CPU time (s)"),
]

# (title, kind, counters.csv column)
PANELS = [
    ("share of huge-page evictions split first (cumulative)", "frac", None),
    ("huge pages swapped out whole (per s)", "rate", "thp_swpout"),
    ("huge pages split before swap-out (per s)", "rate", "thp_swpout_fallback"),
    ("huge pages split, any reason (per s)", "rate", "thp_split_page"),
    ("4 KB pages written to swap (per s)", "rate", "pswpout"),
    ("4 KB pages read from swap (per s)", "rate", "pswpin"),
    ("major page faults (per s)", "rate", "pgmajfault"),
    ("swap in use (MB)", "swapused", None),
    ("memory in huge pages (MB)", "level_mb", "mem_AnonHugePages_kB"),
    ("kswapd CPU (fraction of one core)", "rate", "cpu_kswapd0_s"),
    ("allocation stalls (per s)", "rate", "allocstall"),
    ("node 0 free memory (MB)", "level_mb", "mem_Node0MemFree_kB"),
]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_rows(path):
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def smooth(vals, w):
    """Moving average over the last w values."""
    out, window, total = [], [], 0.0
    for v in vals:
        window.append(v)
        total += v
        if len(window) > w:
            total -= window.pop(0)
        out.append(total / len(window))
    return out


def load_runs(root):
    runs = []
    for path in sorted(glob.glob(os.path.join(root, "*", "run*", "summary.json"))):
        run_dir = os.path.dirname(path)
        with open(path) as f:
            s = json.load(f)
        if "condition" not in s:
            continue
        k = s.get("kernel_deltas", {})
        rec = {"dir": run_dir, "group": s["group"], "cond": s["condition"], "rep": s["rep"],
               "valid": not s.get("validity_problems") and "access_mean_per_s" in s,
               "problems": s.get("validity_problems", []), "window_s": None,
               "t0": s.get("alloc_start_epoch"), "alloc_end_t": None}
        if rec["t0"] is not None and s.get("alloc_end_epoch") is not None:
            rec["alloc_end_t"] = s["alloc_end_epoch"] - rec["t0"]
        for key, _ in METRICS:
            rec[key] = s.get(key) if key in FROM_PROGRAM else k.get(key)
        sw, fb = k.get("thp_swpout"), k.get("thp_swpout_fallback")
        rec["fallback_fraction"] = fb / (sw + fb) if sw is not None and fb is not None and sw + fb > 0 else None
        vals = [num(r["acc_per_s"]) for r in read_rows(os.path.join(run_dir, "memhog.csv"))]
        vals = [v for v in vals if v is not None]
        if len(vals) >= 20:
            w = max(10, min(120, len(vals) // 4))
            rec["window_s"] = w
            rec["acc_first"] = statistics.mean(vals[:w])
            rec["acc_last"] = statistics.mean(vals[-w:])
            rec["acc_trend"] = rec["acc_last"] / rec["acc_first"] if rec["acc_first"] else None
        runs.append(rec)
    return runs


def median_range(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return (statistics.median(vals), min(vals), max(vals)) if vals else None


def summarize(runs):
    groups = {}
    for r in runs:
        if r["valid"]:
            groups.setdefault((r["group"], r["cond"]), []).append(r)
    summary = {}
    for key, rs in groups.items():
        summary[key] = {"n": len(rs)}
        for metric, _ in METRICS:
            summary[key][metric] = median_range([r[metric] for r in rs])
    return summary


def print_and_save(runs, summary, outdir):
    bad = [r for r in runs if not r["valid"]]
    print("runs found: %d, excluded as invalid: %d" % (len(runs), len(bad)))
    for r in bad:
        print("  excluded:", r["dir"], "|", "; ".join(r["problems"]) or "no access data")
    with open(os.path.join(outdir, "summary_conditions.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "condition", "metric", "median", "min", "max", "n_runs"])
        for group in sorted({g for g, _ in summary}):
            conds = [c for c in CONDITIONS if (group, c) in summary]
            window = next((r["window_s"] for r in runs if r["group"] == group and r["window_s"]), "?")
            print("\n=== %s (medians over valid repetitions; first/last window = %s s) ===" % (group, window))
            print("%-50s" % "" + "".join(" %22s" % LABELS[c] for c in conds))
            print("%-50s" % "valid runs" + "".join(" %22d" % summary[(group, c)]["n"] for c in conds))
            for key, label in METRICS:
                cells = []
                for c in conds:
                    m = summary[(group, c)][key]
                    cells.append("%22s" % ("-" if m is None else "%.4g" % m[0]))
                    if m is not None:
                        w.writerow([group, c, key, m[0], m[1], m[2], summary[(group, c)]["n"]])
                print("%-50s %s" % (label, " ".join(cells)))
            base = summary.get((group, "thp-off"))
            if base:
                print("\nspeed relative to THP off (above 1 = faster than THP off):")
                for c in conds:
                    if c == "thp-off":
                        continue
                    for key, name in (("access_mean_per_s", "whole step"), ("acc_last", "last window")):
                        a, b = summary[(group, c)][key], base[key]
                        if a and b and b[0]:
                            print("  %-24s %-12s %.2fx" % (LABELS[c], name, a[0] / b[0]))


def get_plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        print("matplotlib is not installed, so plots were skipped (sudo apt install python3-matplotlib)")
        return None


def plot_throughput(plt, group, runs, outdir):
    fig, ax = plt.subplots(figsize=(9, 5))
    handles = {}
    for r in runs:
        rows = read_rows(os.path.join(r["dir"], "memhog.csv"))
        if not rows or r["t0"] is None:
            continue
        t = [float(x["epoch"]) - r["t0"] for x in rows]
        y = smooth([float(x["acc_per_s"]) for x in rows], 10)
        (line,) = ax.plot(t, y, color=COLORS[r["cond"]], alpha=0.8)
        handles.setdefault(r["cond"], line)
    ax.set_xlabel("seconds since memhog started allocating")
    ax.set_ylabel("accesses per second (10 s average)")
    ax.set_title("Experiment 3, %s" % group)
    ax.set_ylim(bottom=0)
    ax.legend([handles[c] for c in CONDITIONS if c in handles],
              [LABELS[c] for c in CONDITIONS if c in handles])
    fig.savefig(os.path.join(outdir, "throughput_%s.png" % group), dpi=150, bbox_inches="tight")
    plt.close(fig)


def panel_xy(rows, t, kind, key):
    """x and y values of one panel for one run."""
    if kind == "frac":
        sw0, fb0 = num(rows[0].get("thp_swpout")), num(rows[0].get("thp_swpout_fallback"))
        xs, ys = [], []
        for ti, r in zip(t, rows):
            sw, fb = num(r.get("thp_swpout")), num(r.get("thp_swpout_fallback"))
            if None in (sw, fb, sw0, fb0):
                continue
            total = (sw - sw0) + (fb - fb0)
            if total > 0:
                xs.append(ti)
                ys.append((fb - fb0) / total)
        return xs, ys
    if kind == "swapused":
        f0 = num(rows[0].get("mem_SwapFree_kB"))
        pairs = [(ti, num(r.get("mem_SwapFree_kB"))) for ti, r in zip(t, rows)]
        pairs = [(ti, (f0 - v) / 1024.0) for ti, v in pairs if v is not None and f0 is not None]
        return [p[0] for p in pairs], [p[1] for p in pairs]
    vals = [num(r.get(key)) for r in rows]
    if None in vals:
        return [], []
    if kind == "level_mb":
        return t, [v / 1024.0 for v in vals]
    xs, ys = [], []                                   # kind == "rate"
    for i in range(1, len(vals)):
        dt = t[i] - t[i - 1]
        if dt > 0:
            xs.append(t[i])
            ys.append((vals[i] - vals[i - 1]) / dt)
    return xs, smooth(ys, 10)


def plot_counters(plt, group, runs, outdir):
    fig, axes = plt.subplots(4, 3, figsize=(16, 15))
    handles = {}
    for r in runs:
        rows = read_rows(os.path.join(r["dir"], "counters.csv"))
        if len(rows) < 3 or r["t0"] is None:
            continue
        t = [float(x["epoch"]) - r["t0"] for x in rows]
        for ax, (title, kind, key) in zip(axes.flat, PANELS):
            xs, ys = panel_xy(rows, t, kind, key)
            if not xs:
                continue
            (line,) = ax.plot(xs, ys, color=COLORS[r["cond"]], alpha=0.8)
            handles.setdefault(r["cond"], line)
            if r["alloc_end_t"] is not None:
                ax.axvline(r["alloc_end_t"], color=COLORS[r["cond"]], ls=":", alpha=0.4)
    for ax, (title, _, _) in zip(axes.flat, PANELS):
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("seconds since memhog started allocating", fontsize=8)
    fig.legend([handles[c] for c in CONDITIONS if c in handles],
               [LABELS[c] for c in CONDITIONS if c in handles], loc="lower center", ncol=4)
    fig.suptitle("Experiment 3, %s (dotted lines: end of the allocation step)" % group)
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    fig.savefig(os.path.join(outdir, "counters_%s.png" % group), dpi=130, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(HERE, "results", "exp3_pressure"))
    a = ap.parse_args()
    runs = load_runs(a.root)
    if not runs:
        raise SystemExit("no summary.json files from exp3.py found under " + a.root)
    outdir = os.path.join(a.root, "analysis")
    os.makedirs(outdir, exist_ok=True)
    summary = summarize(runs)
    print_and_save(runs, summary, outdir)
    plt = get_plt()
    if plt:
        for group in sorted({r["group"] for r in runs}):
            valid = [r for r in runs if r["group"] == group and r["valid"]]
            if valid:
                plot_throughput(plt, group, valid, outdir)
                plot_counters(plt, group, valid, outdir)
    print("\nsaved to", outdir)


if __name__ == "__main__":
    main()