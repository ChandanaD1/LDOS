#!/usr/bin/env python3
"""
exp3.py -- Experiment 3: uniform random access under global memory pressure, swap on.

QUESTION: when node 0 has less free memory than the array needs, how do THP off and THP on
(with each defrag setting) compare over time? Do huge pages keep being swapped out whole, or
do more and more of them have to be split first?

DESIGN: a helper program (filler) locks most of node 0's memory, so memhog gets about
--avail-pct % of its array (plus --margin-mb). Everything else is the same in every condition:
swap is emptied before every run, each swap-in is a single 4 KB page (page-cluster 0), and memhog
is pinned to node 0. Conditions are interleaved.

USAGE:
  sudo python3 exp3.py --quick                       # 1 GB array, 60 s, one repetition (harness test)
  sudo python3 exp3.py --reps 1 --seconds 300        # pilot
  sudo python3 exp3.py                               # 8 GB, 70% available, 10 minutes, 3 repetitions

RESULTS: results/exp3_pressure/<size>_<avail>pct_<condition>/run<k>/  (same files as before, minus perf.txt)
"""
import argparse
import os
import sys
import time

import common

# condition -> (THP enabled, defrag). With THP off the defrag knob does nothing; it is left at the default.
CONDITIONS = {
    "thp-off":       ("never",  "madvise"),
    "defrag-never":  ("always", "never"),
    "defrag-defer":  ("always", "defer"),
    "defrag-always": ("always", "always"),
}


def check_validity_pressure(s, thp, target_free_mb, free_after_fill_mb, swap_used_before_kb, oom_delta):
    problems = []
    if free_after_fill_mb is not None:
        tol = max(300.0, 0.1 * target_free_mb)
        if abs(free_after_fill_mb - target_free_mb) > tol:
            problems.append("node free memory after the filler was %.0f MB, target %d MB"
                            % (free_after_fill_mb, target_free_mb))
    if swap_used_before_kb != 0:
        problems.append("swap was not empty at the start (%d kB used)" % swap_used_before_kb)
    if oom_delta > 0:
        problems.append("the kernel log shows an out-of-memory event during the run")
    k = s.get("kernel_deltas")
    if not k:
        return problems
    if k.get("pswpout", 0) <= 0:
        problems.append("pswpout did not rise: nothing was swapped out, so there was no pressure")
    huge = k.get("thp_fault_alloc", 0)
    if thp == "always" and huge <= 0:
        problems.append("thp_fault_alloc did not rise although THP is on")
    if thp == "never" and huge > 10:
        problems.append("thp_fault_alloc rose by %g although THP is off" % huge)
    return problems


def run_one(run_dir, cond, rep, a, group):
    thp, defrag = CONDITIONS[cond]
    target_free_mb = int(a.size_mb * a.avail_pct / 100) + a.margin_mb

    # Setup that must succeed (a failure here stops the whole script).
    common.cleanup_leftovers()
    common.reset_swap()
    common.prepare_run(thp, defrag)
    got = common.read_file(common.THP + "/defrag")
    if "[%s]" % defrag not in got:
        sys.exit("THP defrag is '%s' but we asked for '%s'" % (got, defrag))
    swap_used_before_kb = sum(u for _, _, u in common.swap_devices())
    free_before_mb = common.node_free_mb(a.numa_node) or 0
    oom_before = common.oom_count()

    s, problems, filler, free_after_fill_mb = {}, [], None, None
    try:
        filler, fill_info = common.start_filler(target_free_mb, a.numa_node)
        time.sleep(3)
        free_after_fill_mb = common.node_free_mb(a.numa_node)
        cfg = os.path.join(run_dir, "config.txt")
        common.record_config(cfg, {"experiment": "exp3_pressure", "condition": cond, "thp": thp,
                                   "defrag": defrag, "size_mb": a.size_mb, "avail_pct": a.avail_pct,
                                   "margin_mb": a.margin_mb, "target_free_mb": target_free_mb,
                                   "node_free_before_fill_mb": "%.0f" % free_before_mb,
                                   "node_free_after_fill_mb": "%.0f" % (free_after_fill_mb or -1),
                                   "seconds": a.seconds, "repetition": rep})
        common.append_config(cfg, common.pressure_state_text(a.numa_node))
        s = common.run_memhog(run_dir, a.size_mb, a.seconds, numa_node=a.numa_node,
                              sampler_node=1 - a.numa_node, perf_events=None)
    except Exception as e:                       # e.g. memhog was killed or the filler failed
        problems.append("run failed: %s" % e)
    finally:
        common.stop_filler(filler)
        common.cleanup_leftovers()
        common.wait_for_free(a.numa_node, 0.9 * free_before_mb)

    s.update({"group": group, "condition": cond, "thp": thp, "defrag": defrag, "rep": rep,
              "size_mb": a.size_mb, "avail_pct": a.avail_pct, "margin_mb": a.margin_mb,
              "target_free_mb": target_free_mb, "node_free_after_fill_mb": free_after_fill_mb})
    problems += check_validity_pressure(s, thp, target_free_mb, free_after_fill_mb,
                                        swap_used_before_kb, common.oom_count() - oom_before)
    s["validity_problems"] = problems
    common.save_summary(run_dir, s)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="1 GB array, 60 s, 1 repetition (harness test)")
    ap.add_argument("--size-mb", type=int, default=8192)
    ap.add_argument("--avail-pct", type=int, default=70, help="memory left for memhog, %% of its array")
    ap.add_argument("--margin-mb", type=int, default=0, help="extra free memory on top of --avail-pct")
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--conditions", nargs="+", default=list(CONDITIONS), choices=list(CONDITIONS))
    ap.add_argument("--numa-node", type=int, default=0)
    ap.add_argument("--page-cluster", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="skip the swap-size check")
    a = ap.parse_args()
    if a.quick:
        a.size_mb, a.seconds, a.reps = 1024, 60, 1

    common.require_root()
    common.setup_environment()
    common.setup_swap_environment(a.page_cluster)

    needed_mb = a.size_mb * (100 - a.avail_pct) / 100.0
    have_mb = sum(size for _, size, _ in common.swap_devices()) / 1024.0
    if have_mb < 1.5 * needed_mb and not a.force:
        sys.exit("swap is %.0f MB but about %.0f MB must be swapped out; use --force to run anyway"
                 % (have_mb, needed_mb))

    label = "%dgb" % (a.size_mb // 1024) if a.size_mb % 1024 == 0 else "%dmb" % a.size_mb
    group = "%s_%dpct" % (label, a.avail_pct) + ("_m%d" % a.margin_mb if a.margin_mb else "")
    root = os.path.join(common.HERE, "results", "exp3_pressure" + ("_quick" if a.quick else ""))

    total = a.reps * len(a.conditions)
    print("%d runs, about %.0f minutes in total" % (total, total * (a.seconds + 300) / 60.0), flush=True)
    done = 0
    for rep in range(1, a.reps + 1):
        for cond in a.conditions:
            done += 1
            run_dir = os.path.join(root, "%s_%s" % (group, cond), "run%d" % rep)
            os.makedirs(run_dir, exist_ok=True)
            print("[%d/%d] %s, %s, repetition %d ..." % (done, total, group, cond, rep), flush=True)
            s = run_one(run_dir, cond, rep, a, group)
            k = s.get("kernel_deltas", {})
            print("    alloc %.0f s | %.0f accesses/s | resident %.0f MB, swapped %.0f MB | "
                  "huge swap-outs %g, split first %g | %s"
                  % (s.get("alloc_total_s", 0), s.get("access_mean_per_s", 0),
                     s.get("end_rss_mb", 0) if isinstance(s.get("end_rss_mb"), float) else 0,
                     s.get("end_swap_mb", 0) if isinstance(s.get("end_swap_mb"), float) else 0,
                     k.get("thp_swpout", 0), k.get("thp_swpout_fallback", 0),
                     "VALID" if not s["validity_problems"] else "PROBLEMS: " + "; ".join(s["validity_problems"])),
                  flush=True)
    print("done. results in", root)


if __name__ == "__main__":
    main()