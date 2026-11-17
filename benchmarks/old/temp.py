#!/usr/bin/env python3
"""
run_exp.py -- ONE run of memhog under controlled conditions, with everything
recorded. Used for E1 (no pressure), E2/E2b (cgroup limit), later E3.

Examples (arguments after "--" go to memhog):
  # E1: THP on, no swap, 16GB array, 60s
  sudo ./run_exp.py --label e1_on  --thp always --no-swap -- --size-mb 16384 --duration 60
  sudo ./run_exp.py --label e1_off --thp never  --no-swap -- --size-mb 16384 --duration 60

  # E2: 8GB array limited to 6GB of RAM (cgroup), swap on and emptied first
  sudo ./run_exp.py --label e2_on_75 --thp always --reset-swap --mem-limit-mb 6144 \
       -- --size-mb 8192 --duration 300

  # E1b: khugepaged experiment (madvise after allocation + scan-speed knob)
  sudo ./run_exp.py --label e1b --thp madvise --no-swap \
       --set /sys/kernel/mm/transparent_hugepage/khugepaged/pages_to_scan=4096 \
       -- --size-mb 16384 --duration 300 --advise huge --advise-when after
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
THP = "/sys/kernel/mm/transparent_hugepage"
CG_ROOT = "/sys/fs/cgroup"


def write(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "<unreadable>"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def record_config(outdir):
    """Save every knob that could change the result, so a run is reproducible."""
    lines = ["kernel: " + os.uname().release, "cmdline: " + read("/proc/cmdline")]
    knobs = [THP + p for p in ("/enabled", "/defrag", "/use_zero_page", "/shrink_underused",
                               "/hugepages-2048kB/enabled",
                               "/khugepaged/defrag", "/khugepaged/pages_to_scan",
                               "/khugepaged/scan_sleep_millisecs", "/khugepaged/alloc_sleep_millisecs",
                               "/khugepaged/max_ptes_none", "/khugepaged/max_ptes_swap",
                               "/khugepaged/max_ptes_shared")]
    knobs += ["/proc/sys/vm/" + p for p in ("swappiness", "page-cluster", "min_free_kbytes",
                                            "watermark_scale_factor", "overcommit_memory")]
    for k in knobs:
        lines.append("%s = %s" % (k, read(k)))
    lines += ["", "--- swap ---", read("/proc/swaps"), "", "--- lscpu ---", sh("lscpu"),
              "", "--- numa ---", sh("numactl -H"), "", "--- lsblk ---", sh("lsblk -d -o NAME,SIZE,ROTA,MODEL")]
    with open(os.path.join(outdir, "config.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--thp", choices=["always", "madvise", "never"], required=True)
    ap.add_argument("--defrag", choices=["always", "defer", "defer+madvise", "madvise", "never"])
    ap.add_argument("--mem-limit-mb", type=int, help="cgroup memory.max (omit = no cgroup)")
    ap.add_argument("--swap-limit-mb", type=int, help="cgroup memory.swap.max (default: unlimited)")
    ap.add_argument("--no-swap", action="store_true", help="swapoff -a first (E1)")
    ap.add_argument("--reset-swap", action="store_true", help="swapoff -a; swapon -a first (E2: empty swap)")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                    help="extra knob to write before the run (repeatable)")
    ap.add_argument("--perf-events", help="e.g. dTLB-load-misses,dTLB-loads (per-second, via perf stat -I)")
    ap.add_argument("--memhog", default=os.path.join(HERE, "memhog"))
    ap.add_argument("--outdir")
    ap.add_argument("memhog_args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    margs = a.memhog_args[1:] if a.memhog_args[:1] == ["--"] else a.memhog_args

    if os.geteuid() != 0:
        sys.exit("run as root")
    outdir = a.outdir or os.path.join(HERE, "results", "%s_%s" % (a.label, time.strftime("%Y%m%d_%H%M%S")))
    os.makedirs(outdir, exist_ok=True)

    # 1. identical starting state for every run
    if a.no_swap:
        sh("swapoff -a")
    elif a.reset_swap:
        print(sh("swapoff -a && swapon -a") or "swap reset")
    sh("sync")
    write("/proc/sys/vm/drop_caches", 3)       # drop page cache + slab so file cache doesn't skew memory
    write("/proc/sys/vm/compact_memory", 1)    # defragment RAM into big free blocks

    # 2. the one thing that differs between configurations
    write(THP + "/enabled", a.thp)
    if a.defrag:
        write(THP + "/defrag", a.defrag)
    for kv in a.set:
        path, val = kv.split("=", 1)
        write(path, val)
    record_config(outdir)

    # 3. optional cgroup = "available RAM" for the program
    cg = None
    if a.mem_limit_mb:
        if not os.path.exists(CG_ROOT + "/cgroup.controllers"):
            sys.exit("cgroup v2 not mounted")
        try:
            write(CG_ROOT + "/cgroup.subtree_control", "+memory")
        except OSError:
            pass
        cg = os.path.join(CG_ROOT, "thpexp_" + a.label)
        os.makedirs(cg, exist_ok=True)
        write(cg + "/memory.max", a.mem_limit_mb * 1048576)
        write(cg + "/memory.swap.max", a.swap_limit_mb * 1048576 if a.swap_limit_mb else "max")

    # 4. counters sampler runs alongside
    sampler_cmd = [sys.executable, os.path.join(HERE, "sampler.py"), "--out", os.path.join(outdir, "counters.csv")]
    if cg:
        sampler_cmd += ["--cgroup", cg]
    sampler = subprocess.Popen(sampler_cmd)
    time.sleep(2)   # a few "before" rows

    # 5. the benchmark
    cmd = [a.memhog] + margs
    if "--csv" not in margs:
        cmd += ["--csv", os.path.join(outdir, "memhog.csv")]
    if a.perf_events:
        cmd = ["perf", "stat", "-I", "1000", "-x,", "-e", a.perf_events,
               "-o", os.path.join(outdir, "perf.csv"), "--"] + cmd

    def enter_cgroup():
        write(os.path.join(cg, "cgroup.procs"), os.getpid())

    with open(os.path.join(outdir, "memhog.out"), "w") as fout:
        t0 = time.time()
        p = subprocess.run(cmd, stdout=fout, stderr=subprocess.STDOUT,
                           preexec_fn=enter_cgroup if cg else None)
    print("memhog exit code %d after %.1fs (137 = killed, probably cgroup OOM)" % (p.returncode, time.time() - t0))

    # 6. wrap up
    time.sleep(2)   # a few "after" rows
    sampler.terminate()
    sampler.wait(timeout=10)
    if cg:
        shutil.copy(cg + "/memory.events", os.path.join(outdir, "final_cgroup_events.txt"))
        try:
            os.rmdir(cg)
        except OSError:
            pass
    print(sh("grep RESULT %s" % os.path.join(outdir, "memhog.out")))
    print("outputs in", outdir)


if __name__ == "__main__":
    main()