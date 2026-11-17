#!/usr/bin/env python3
"""
exp1.py -- Experiment 1: baseline. THP always vs. never, with plenty of free memory.

QUESTION: with plenty of memory, how much faster is THP-on for random access over a large
array, and what does it cost (or save) at allocation time?

DESIGN: the only thing that differs between conditions is the THP 'enabled' setting.
Same program, same size, same random seed, pinned to NUMA node 0. Runs are interleaved
(always, never, always, never, ...) so slow drift on the machine affects both equally.

USAGE:
  sudo python3 exp1.py --quick                  # 1 GB, 5 s, one pair of runs: a test of the harness
  sudo python3 exp1.py                          # the real thing: 8 GB, 30 s, 3 repetitions
  sudo python3 exp1.py --sizes 8192 16384       # add a 16 GB sensitivity check

RESULTS: results/exp1_baseline/<size>_<thp>/run<k>/  with
  config.txt   memhog.csv   memhog.out   counters.csv   perf.txt   summary.json
"""
import argparse
import os

import common

THP_SETTINGS = ["always", "never"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="1 GB, 5 s, 1 repetition (harness test)")
    ap.add_argument("--sizes", type=int, nargs="+", default=[8192], help="array sizes in MB")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--numa-node", type=int, default=0)
    ap.add_argument("--perf-events", default="dTLB-loads,dTLB-load-misses")
    ap.add_argument("--no-perf", action="store_true")
    a = ap.parse_args()
    if a.quick:
        a.sizes, a.reps, a.seconds = [1024], 1, 5

    common.require_root()
    common.setup_environment()
    root = os.path.join(common.HERE, "results", "exp1_baseline" + ("_quick" if a.quick else ""))

    total = len(a.sizes) * a.reps * len(THP_SETTINGS)
    done = 0
    for size_mb in a.sizes:
        label = "%dgb" % (size_mb // 1024) if size_mb % 1024 == 0 else "%dmb" % size_mb
        for rep in range(1, a.reps + 1):
            for thp in THP_SETTINGS:                         # interleaved on/off/on/off
                done += 1
                run_dir = os.path.join(root, "%s_%s" % (label, thp), "run%d" % rep)
                os.makedirs(run_dir, exist_ok=True)
                print("[%d/%d] %s, THP %s, repetition %d ..." % (done, total, label, thp, rep), flush=True)

                common.prepare_run(thp)
                common.record_config(os.path.join(run_dir, "config.txt"),
                                     {"experiment": "exp1_baseline", "thp": thp,
                                      "size_mb": size_mb, "seconds": a.seconds, "repetition": rep})
                s = common.run_memhog(run_dir, size_mb, a.seconds, numa_node=a.numa_node,
                                      sampler_node=1 - a.numa_node,
                                      perf_events=None if a.no_perf else a.perf_events)

                s["validity_problems"] = common.check_validity(s, thp, size_mb)
                common.save_summary(run_dir, s)
                print("    alloc %.2f s | %.2f M accesses/s | huge pages %.0f MB | dTLB miss rate %s | %s"
                      % (s["alloc_total_s"], s["access_mean_per_s"] / 1e6, s["after_alloc_anon_huge_mb"],
                         "%.1f%%" % (100 * s["dtlb_miss_rate"]) if "dtlb_miss_rate" in s else "n/a",
                         "VALID" if not s["validity_problems"] else "PROBLEMS: " + "; ".join(s["validity_problems"])),
                      flush=True)
    print("done. results in", root)


if __name__ == "__main__":
    main()