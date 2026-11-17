#!/usr/bin/env python3
"""
sampler.py -- records kernel counters once per second into a CSV.

Why: memhog tells us what the PROGRAM experienced (speed, faults). This tells us what the
KERNEL was doing (huge pages created or split, reclaim, compaction, swap traffic, and how
much CPU the kernel's background threads used). The counters explain a result.

Every value is CUMULATIVE (it only goes up). To see what happened during a run, subtract
the first row from the last; for a per-second rate, subtract consecutive rows.

The 'epoch' column is wall-clock time, the same clock as memhog's CSV, so rows line up.

Usage:  python3 sampler.py --out counters.csv [--interval 1.0]
        (stop it with Ctrl-C or kill; it finishes its current row first)
"""
import argparse
import os
import signal
import time

THP_DIR = "/sys/kernel/mm/transparent_hugepage"
MTHP_DIR = THP_DIR + "/hugepages-2048kB/stats"   # per-size THP stats (path assumes 4 KB base pages)

# From /proc/vmstat (the kernel's own event counters):
VMSTAT_KEYS = [
    "thp_fault_alloc",        # a page fault got a 2 MB huge page directly
    "thp_fault_fallback",     # a huge page was attempted at a fault but a 4 KB page was given instead
    "thp_collapse_alloc",     # khugepaged merged 4 KB pages into a huge page
    "thp_split_page",         # a huge page was split into 4 KB pages (any reason)
    "thp_split_page_failed",  # a split attempt failed, leaving the huge page intact
    "thp_swpout",             # a huge page was written to swap whole
    "thp_swpout_fallback",    # a huge page could not be written whole and was split first
    "pswpin",                 # 4 KB pages read in from swap
    "pswpout",                # 4 KB pages written out to swap
    "pgmajfault",             # page faults that needed a disk read (e.g. a swap-in)
    "pgscan_kswapd",          # pages examined by the background reclaim thread
    "pgscan_direct",          # pages examined by a program's own thread doing reclaim
    "compact_stall",          # times a program had to stall to compact memory itself
    "compact_success",        # ... and the compaction produced the block needed
    "compact_fail",           # ... and it did not
    "pgsteal_kswapd",         # pages actually freed by the background reclaim thread
    "pgsteal_direct",         # pages actually freed by a program's own thread doing reclaim
]
# allocstall_* exists once per memory zone; we add them into one column "allocstall"
# (times an allocation had to stall to reclaim memory directly).

MEMINFO_KEYS = ["AnonHugePages", "MemFree", "SwapFree"]   # kB, instantaneous (not cumulative)
MTHP_KEYS = ["swpin", "swpin_fallback", "swpout", "swpout_fallback", "split", "split_failed"]
KTHREADS = ["kswapd0", "khugepaged", "kcompactd0"]        # kswapd0 = node 0's reclaim thread


def read_vmstat():
    out, stall = {}, 0
    with open("/proc/vmstat") as f:
        for line in f:
            name, val = line.split()
            if name in VMSTAT_KEYS:
                out[name] = int(val)
            elif name.startswith("allocstall"):
                stall += int(val)
    out["allocstall"] = stall
    return out


def read_meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            name, rest = line.split(":", 1)
            if name in MEMINFO_KEYS:
                out[name] = int(rest.split()[0])
    return out


def read_mthp():
    out = {}
    for k in MTHP_KEYS:
        try:
            with open("%s/%s" % (MTHP_DIR, k)) as f:
                out[k] = int(f.read())
        except (OSError, ValueError):
            pass          # not present on this kernel: leave the column empty
    return out


def find_kthreads():
    """Kernel threads live for the whole boot, so we look their PIDs up once."""
    pids = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in KTHREADS:
            pids[comm] = pid
    return pids


def cpu_seconds(pid):
    """Cumulative CPU time (user + system) of one process, in seconds."""
    if pid is None:
        return ""
    try:
        with open("/proc/%s/stat" % pid) as f:
            stat = f.read()
    except OSError:
        return ""
    fields = stat.rsplit(")", 1)[1].split()          # split after the name, which may contain spaces
    ticks = int(fields[11]) + int(fields[12])        # utime + stime, in clock ticks
    return round(ticks / os.sysconf("SC_CLK_TCK"), 3)

def read_node_free(node=0):
    """Free memory of one NUMA node in kB (the whole-machine MemFree hides node 0's pressure)."""
    try:
        with open("/sys/devices/system/node/node%d/meminfo" % node) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "MemFree:":
                    return int(parts[3])
    except OSError:
        pass
    return ""

def snapshot(pids):
    vm, mi, mt = read_vmstat(), read_meminfo(), read_mthp()
    row = {"epoch": round(time.time(), 3)}
    for k in VMSTAT_KEYS + ["allocstall"]:
        row[k] = vm.get(k, "")
    for k in MEMINFO_KEYS:
        row["mem_%s_kB" % k] = mi.get(k, "")
    row["mem_Node0MemFree_kB"] = read_node_free(0)
    for k in MTHP_KEYS:
        row["mthp2M_" + k] = mt.get(k, "")
    for name in KTHREADS:
        row["cpu_%s_s" % name] = cpu_seconds(pids.get(name))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    pids = find_kthreads()
    header = None
    nxt = time.time()
    with open(a.out, "w") as out:
        while not stop:
            row = snapshot(pids)
            if header is None:
                header = list(row.keys())
                out.write(",".join(header) + "\n")
            out.write(",".join(str(row[k]) for k in header) + "\n")
            out.flush()                               # so you can `tail -f` it while it runs
            nxt += a.interval
            delay = nxt - time.time()
            if delay > 0:
                time.sleep(delay)                     # sleep until an exact time: no drift
            else:
                nxt = time.time()


if __name__ == "__main__":
    main()