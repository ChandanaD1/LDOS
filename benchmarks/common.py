#!/usr/bin/env python3
"""
common.py -- shared helpers imported by every experiment script (exp1.py, exp2.py, ...).

Contents: (1) environment setup + configuration capture, (2) running one complete measurement
(memhog + sampler + perf) and writing summary.json.

Everything that changes system settings needs root, so run experiment scripts with sudo.
"""
import csv
import glob
import json
import os
import subprocess
import sys
import time

THP = "/sys/kernel/mm/transparent_hugepage"


# ---------------------------------------------------------------- small helpers

def require_root():
    if os.geteuid() != 0:
        sys.exit("This script changes system settings: run it with sudo.")


def write_file(path, value):
    """Write a value into a kernel knob, e.g. write_file(THP + '/enabled', 'always')."""
    with open(path, "w") as f:
        f.write(str(value))


def read_file(path, default="n/a"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def sh(cmd):
    """Run a shell command and return everything it printed."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- environment setup

def setup_environment():
    """Done ONCE per experiment: remove noise sources that are not part of what we measure."""
    # No automatic page migration between NUMA nodes (we only use node 0 anyway).
    try:
        write_file("/proc/sys/kernel/numa_balancing", 0)
    except OSError:
        pass                                   # kernel built without it: nothing to turn off

    # Fix the CPU clock at its maximum so a run is not affected by the clock ramping up or down.
    governors = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    done = 0
    for g in governors:
        try:
            write_file(g, "performance")
            done += 1
        except OSError:
            pass
    print("setup: numa_balancing=%s, governor=performance on %d of %d CPUs"
          % (read_file("/proc/sys/kernel/numa_balancing"), done, len(governors)))


def prepare_run(thp, defrag=None):
    """Done before EVERY run: an identical starting state, then the one knob that differs."""
    sh("sync")                                          # flush pending file writes to disk
    write_file("/proc/sys/vm/drop_caches", 3)           # drop the file cache and kernel object caches
    write_file("/proc/sys/vm/compact_memory", 1)        # compact RAM into big free blocks
    write_file(THP + "/enabled", thp)                   # THE knob for Experiment 1
    if defrag:
        write_file(THP + "/defrag", defrag)

    # Read the knob back: a setting that silently did not apply would ruin the comparison.
    got = read_file(THP + "/enabled")
    if "[%s]" % thp not in got:
        sys.exit("THP enabled is '%s' but we asked for '%s'" % (got, thp))

    time.sleep(2)     # let background threads (khugepaged, kcompactd) settle after the above


# ---------------------------------------------------------------- configuration capture

def record_config(path, extra=None):
    """Save the state of the system for this run, so any result can be traced to its settings."""
    lines = ["time: " + time.strftime("%Y-%m-%d %H:%M:%S"),
             "kernel: " + os.uname().release]
    for k, v in (extra or {}).items():
        lines.append("%s: %s" % (k, v))
    lines.append("")
    knobs = [THP + "/enabled", THP + "/defrag", THP + "/shrink_underused",
             THP + "/khugepaged/defrag", THP + "/khugepaged/pages_to_scan",
             THP + "/khugepaged/scan_sleep_millisecs",
             "/proc/sys/vm/swappiness", "/proc/sys/vm/page-cluster",
             "/proc/sys/kernel/numa_balancing",
             "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"]
    for k in knobs:
        lines.append("%s = %s" % (k, read_file(k)))
    lines += ["", "--- swap ---", read_file("/proc/swaps"),
              "", "--- numa ---", sh("numactl -H"),
              "", "--- memory (MB) ---", sh("free -m")]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- running one measurement

HERE = os.path.dirname(os.path.abspath(__file__))
MEMHOG = os.path.join(HERE, "memhog")
SAMPLER = os.path.join(HERE, "sampler.py")
PERF = "/usr/lib/linux-tools/6.8.0-149-generic/perf"   # where we installed perf (check this path)


def to_number(text):
    """'8192.0' -> 8192.0, but leave words such as 'unavailable' alone."""
    try:
        return float(text)
    except ValueError:
        return text


def counter_deltas(counters_csv):
    """How much each cumulative kernel counter changed between the first and last row.
    Returns (deltas, peak_anon_huge_mb). The mem_* columns are snapshots, not cumulative,
    so they are skipped here (we keep only the peak of AnonHugePages)."""
    with open(counters_csv) as f:
        rows = list(csv.DictReader(f))
    first, last = rows[0], rows[-1]
    deltas = {}
    for key in first:
        if key == "epoch" or key.startswith("mem_") or first[key] == "" or last[key] == "":
            continue
        deltas[key] = round(float(last[key]) - float(first[key]), 3)
    peak_kb = max(float(r["mem_AnonHugePages_kB"]) for r in rows if r["mem_AnonHugePages_kB"] != "")
    return deltas, peak_kb / 1024.0


def parse_perf(path):
    """Read 'perf stat -x,' output: lines like '263577,,dTLB-loads,1000000000,100.00,,'."""
    counts = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return counts
    for line in lines:
        parts = line.split(",")
        if line.startswith("#") or len(parts) < 3:
            continue
        try:
            counts[parts[2]] = int(parts[0])
        except ValueError:
            pass                                  # e.g. '<not supported>'
    return counts


def run_memhog(run_dir, size_mb, seconds, write_pct=50, numa_node=0, sampler_node=1,
               perf_events=None, perf_path=None, extra_args=None):
    """One complete measurement. Leaves memhog.csv, memhog.out, counters.csv, perf.txt and
    summary.json in run_dir, and returns the summary as a dict."""
    perf_path = perf_path or PERF
    os.makedirs(run_dir, exist_ok=True)
    counters_csv = os.path.join(run_dir, "counters.csv")
    perf_txt = os.path.join(run_dir, "perf.txt")

    # 1. Start the sampler first (on another NUMA node, so it never competes with the worker).
    sampler_cmd = [sys.executable, SAMPLER, "--out", counters_csv]
    if sampler_node is not None:
        sampler_cmd = ["numactl", "--cpunodebind=%d" % sampler_node] + sampler_cmd
    sampler = subprocess.Popen(sampler_cmd)
    time.sleep(2)                                  # a couple of "before" rows

    # 2. Start memhog on the chosen NUMA node, and read its output line by line as it runs.
    cmd = [MEMHOG, "--size-mb", str(size_mb), "--seconds", str(seconds),
           "--write-pct", str(write_pct), "--csv", os.path.join(run_dir, "memhog.csv")]
    cmd += (extra_args or [])
    if numa_node is not None:
        cmd = ["numactl", "--cpunodebind=%d" % numa_node, "--membind=%d" % numa_node] + cmd
    values, perf_proc = {}, None
    with open(os.path.join(run_dir, "memhog.out"), "w") as out:
        # numactl replaces itself with the program it launches, so p.pid is memhog's own PID.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        for line in p.stdout:
            out.write(line)
            if "=" in line:
                key, val = line.strip().split("=", 1)
                values[key] = to_number(val)
            # memhog prints this line the moment allocation ends and the access step begins:
            # that is when we start counting TLB events, so allocation does not pollute them.
            if perf_events and perf_proc is None and line.startswith("after_alloc_anon_huge_mb"):
                perf_proc = subprocess.Popen(
                    [perf_path, "stat", "-p", str(p.pid), "-e", perf_events, "-x", ",",
                     "-o", perf_txt, "--", "sleep", str(max(seconds - 1, 1))],
                    stderr=subprocess.DEVNULL)
        p.wait()
    if perf_proc:
        perf_proc.wait(timeout=60)

    # 3. A couple of "after" rows, then stop the sampler.
    time.sleep(2)
    sampler.terminate()
    sampler.wait(timeout=10)
    if p.returncode != 0:
        raise RuntimeError("memhog exited with code %d (see %s/memhog.out)" % (p.returncode, run_dir))

    # 4. Gather everything into one summary.
    summary = dict(values)
    summary["kernel_deltas"], summary["peak_anon_huge_mb_system"] = counter_deltas(counters_csv)
    if perf_events:
        summary["perf"] = parse_perf(perf_txt)
        loads, misses = summary["perf"].get("dTLB-loads"), summary["perf"].get("dTLB-load-misses")
        if loads and misses is not None:
            summary["dtlb_miss_rate"] = misses / loads
    save_summary(run_dir, summary)
    return summary


def save_summary(run_dir, summary):
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

def check_validity(s, thp, size_mb):
    """Was this run usable? Returns a list of problems (empty = fine)."""
    problems = []
    k = s["kernel_deltas"]
    expected_huge_pages = size_mb // 2                      # one 2 MB page per 2 MB of array
    huge_mb = s.get("after_alloc_anon_huge_mb")
    if not isinstance(huge_mb, float):
        problems.append("could not read AnonHugePages from the program")
    elif thp == "always":
        if huge_mb < 0.99 * size_mb:
            problems.append("only %.0f of %d MB is backed by huge pages" % (huge_mb, size_mb))
        if k.get("thp_fault_alloc", 0) < 0.95 * expected_huge_pages:
            problems.append("thp_fault_alloc rose by only %g (expected about %d)"
                            % (k.get("thp_fault_alloc", 0), expected_huge_pages))
        if k.get("thp_fault_fallback", 0) > 0.01 * expected_huge_pages:
            problems.append("thp_fault_fallback rose by %g: huge-page attempts were failing"
                            % k["thp_fault_fallback"])
    else:
        if huge_mb > 1:
            problems.append("%.0f MB is in huge pages although THP is never" % huge_mb)
        if k.get("thp_fault_alloc", 0) > 10:
            problems.append("thp_fault_alloc rose by %g although THP is never" % k["thp_fault_alloc"])
    if s.get("access_majflt", 0) > 5:
        problems.append("access_majflt=%g: pages were being read from disk" % s["access_majflt"])
    if k.get("compact_stall", 0) > 5:
        problems.append("compact_stall rose by %g: compaction was happening" % k["compact_stall"])
    if k.get("cpu_kswapd0_s", 0) > 0.5:
        problems.append("kswapd0 used %.2f s of CPU: reclaim was running" % k["cpu_kswapd0_s"])
    return problems


# ============================= memory-pressure helpers (Experiment 3) ===================================

FILLER = os.path.join(HERE, "filler")
SWAP_READAHEAD = "/proc/sys/vm/page-cluster"


def setup_swap_environment(page_cluster=0):
    """Done ONCE per swap experiment. page-cluster is log2 of how many neighbouring pages the kernel
    reads on a swap-in; 0 means each swap-in reads exactly one 4 KB page."""
    write_file(SWAP_READAHEAD, page_cluster)
    print("setup: page-cluster=%s" % read_file(SWAP_READAHEAD))


def swap_devices():
    """(name, size_kb, used_kb) for every active swap area, from /proc/swaps."""
    out = []
    for line in read_file("/proc/swaps", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            out.append((parts[0], int(parts[2]), int(parts[3])))
    return out


def reset_swap():
    """Empty the swap area: swap off, then swap the same device(s) back on. Run it only when no
    program is using swap. Raises RuntimeError if it cannot restore swap."""
    devices = [name for name, _, _ in swap_devices()]
    if not devices:
        raise RuntimeError("no swap is active, but this experiment needs swap on")
    r = subprocess.run(["swapoff", "-a"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("swapoff -a failed: " + r.stderr.strip())
    for name in devices:
        r = subprocess.run(["swapon", name], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("swapon %s failed: %s" % (name, r.stderr.strip()))
    used = sum(u for _, _, u in swap_devices())
    if used != 0:
        raise RuntimeError("swap is not empty after the reset (%d kB used)" % used)


def node_free_mb(node=0):
    """Free memory of one NUMA node in MB."""
    for line in read_file("/sys/devices/system/node/node%d/meminfo" % node, "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "MemFree:":
            return int(parts[3]) / 1024.0
    return None


def oom_count():
    """How many 'out of memory' messages the kernel log holds (to detect an out-of-memory kill)."""
    out = sh("dmesg 2>/dev/null | grep -ci 'out of memory'")
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def cleanup_leftovers():
    """Stop anything a previous (interrupted) run may have left behind."""
    for args in (["pkill", "-x", "memhog"], ["pkill", "-x", "filler"], ["pkill", "-f", "sampler.py"]):
        subprocess.run(args, capture_output=True)
    time.sleep(1)


def start_filler(target_free_mb, node=0):
    """Start the filler on `node`; returns (process, what it printed) once it prints READY."""
    cmd = ["numactl", "--cpunodebind=%d" % node, "--membind=%d" % node,
           FILLER, "--node", str(node), "--target-free-mb", str(int(target_free_mb))]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    info = {}
    for line in p.stdout:
        line = line.strip()
        if "=" in line:
            key, val = line.split("=", 1)
            info[key] = to_number(val)
        if line == "READY":
            return p, info
    p.wait()
    raise RuntimeError("the filler exited before it was ready (exit code %s)" % p.returncode)


def stop_filler(p):
    if p is None:
        return
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def wait_for_free(node, at_least_mb, timeout_s=180):
    """Wait until the node's free memory has come back (memory is returned a little after a program exits)."""
    start = time.time()
    while time.time() - start < timeout_s:
        free = node_free_mb(node)
        if free is not None and free >= at_least_mb:
            return True
        time.sleep(2)
    print("warning: node %d free memory did not recover within %d s" % (node, timeout_s))
    return False


def pressure_state_text(node=0):
    """Text for config.txt: the node's memory, its watermarks (the free-memory thresholds that wake
    background reclaim), and the knobs that affect swapping."""
    lines = ["", "--- node %d memory (after the filler) ---" % node,
             read_file("/sys/devices/system/node/node%d/meminfo" % node),
             "", "--- node %d zone watermarks, in 4 KB pages ---" % node]
    zone = None
    for line in read_file("/proc/zoneinfo", "").splitlines():
        if line.startswith("Node %d, zone" % node):
            zone = line.split(",", 1)[1].strip()
        elif line.startswith("Node "):
            zone = None
        elif zone and line.split()[:1] in (["min"], ["low"], ["high"], ["managed"]):
            lines.append("%s %s" % (zone, " ".join(line.split())))
    lines += ["", "--- memory knobs ---"]
    for k in ("min_free_kbytes", "watermark_scale_factor", "watermark_boost_factor",
              "swappiness", "page-cluster", "overcommit_memory"):
        lines.append("%s = %s" % (k, read_file("/proc/sys/vm/" + k)))
    return "\n".join(lines)


def append_config(path, text):
    with open(path, "a") as f:
        f.write(text + "\n")