#!/usr/bin/env python3
"""
exp2.py -- Experiment 2: does the THP benefit depend on the access pattern?

Same as Experiment 1 (THP always vs. never, plenty of free memory, node 0), repeated for
four access patterns: random, sequential, hot-contig, hot-scatter.
The hot patterns send --hot-pct % of accesses to the same number of hot pages (one per
2 MB region); only their PLACEMENT differs (one packed block vs. spread over the array).

USAGE:
  sudo python3 exp2.py --quick      # 1 GB, 5 s, one repetition: a test of the harness
  sudo python3 exp2.py              # real run: 8 GB, 30 s, 3 repetitions (about 20 minutes)
  python3 analyze_exp1.py --root results/exp2_patterns

RESULTS: results/exp2_patterns/<size>_<pattern>_<thp>/run<k>/  (same files as Experiment 1)
"""
import argparse
import os

import common

THP_SETTINGS = ["always", "never"]
PATTERNS = ["random", "sequential", "hot-contig", "hot-scatter"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="1 GB, 5 s, 1 repetition (harness test)")
    ap.add_argument("--size-mb", type=int, default=8192)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--patterns", nargs="+", default=PATTERNS, choices=PATTERNS)
    ap.add_argument("--hot-pct", type=int, default=90)
    ap.add_argument("--seq-step", type=int, default=64)
    ap.add_argument("--numa-node", type=int, default=0)
    ap.add_argument("--perf-events", default="dTLB-loads,dTLB-load-misses")
    ap.add_argument("--no-perf", action="store_true")
    a = ap.parse_args()
    if a.quick:
        a.size_mb, a.reps, a.seconds = 1024, 1, 5

    common.require_root()
    common.setup_environment()
    root = os.path.join(common.HERE, "results", "exp2_patterns" + ("_quick" if a.quick else ""))
    label = "%dgb" % (a.size_mb // 1024) if a.size_mb % 1024 == 0 else "%dmb" % a.size_mb

    total = a.reps * len(a.patterns) * len(THP_SETTINGS)
    done = 0
    for rep in range(1, a.reps + 1):
        for pattern in a.patterns:                       # interleaved: every pattern and setting
            for thp in THP_SETTINGS:                     # is sampled in each round
                done += 1
                run_dir = os.path.join(root, "%s_%s_%s" % (label, pattern, thp), "run%d" % rep)
                os.makedirs(run_dir, exist_ok=True)
                print("[%d/%d] %s, %s, THP %s, repetition %d ..." % (done, total, label, pattern, thp, rep),
                      flush=True)

                common.prepare_run(thp)
                common.record_config(os.path.join(run_dir, "config.txt"),
                                     {"experiment": "exp2_patterns", "thp": thp, "pattern": pattern,
                                      "size_mb": a.size_mb, "seconds": a.seconds, "repetition": rep,
                                      "hot_pct": a.hot_pct, "seq_step": a.seq_step})
                s = common.run_memhog(run_dir, a.size_mb, a.seconds, numa_node=a.numa_node,
                                      sampler_node=1 - a.numa_node,
                                      perf_events=None if a.no_perf else a.perf_events,
                                      extra_args=["--pattern", pattern, "--hot-pct", str(a.hot_pct),
                                                  "--seq-step", str(a.seq_step)])

                s["validity_problems"] = common.check_validity(s, thp, a.size_mb)
                common.save_summary(run_dir, s)
                print("    alloc %.2f s | %.2f M accesses/s | huge pages %.0f MB | dTLB miss rate %s | %s"
                      % (s["alloc_total_s"], s["access_mean_per_s"] / 1e6, s["after_alloc_anon_huge_mb"],
                         "%.1f%%" % (100 * s["dtlb_miss_rate"]) if "dtlb_miss_rate" in s else "n/a",
                         "VALID" if not s["validity_problems"] else "PROBLEMS: " + "; ".join(s["validity_problems"])),
                      flush=True)
    print("done. results in", root)


if __name__ == "__main__":
    main()