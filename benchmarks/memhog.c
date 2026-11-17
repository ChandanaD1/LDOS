/*

memhog.c - the shared workload for the THP-vs-reclaim experiments
- Allocation step: map the array, touch every 4 KB page once, measure time (fault cost)
- Access step: random reads/writes inside the same array, measure accesses per second, over 30 s

BUILD STATE: chunks 1-4 = options + allocation step + validity check (AnonHugePages) + access step
    + per-second reporter thread.
    Build with: gcc -O2 -Wall -pthread -o memhog memhog.c

Usage: ./memhog [--size-mb N] [--seconds N] [--write-pct P] [--seed S] [--csv FILE]

Output: lines of the form key=value on stdout, so a script can parse them.
 
*/


#define _GNU_SOURCE
#include <getopt.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#include <string.h>

#define PAGE 4096UL                  // normal page size
#define HUGE (2UL * 1024 * 1024)     // 2 MB = one huge page
#define PAGES_PER_HUGE (HUGE / PAGE) // 512 small pages in one huge page

enum pattern { PAT_RANDOM, PAT_SEQ, PAT_HOT_CONTIG, PAT_HOT_SCATTER };
static const char *PAT_NAME[] = { "random", "sequential", "hot-contig", "hot-scatter" };

// config settings
struct config {
    size_t size_mb;       // array size
    int seconds;          // length of the access step
    int write_pct;        // % of accesses that also write
    uint64_t seed;        // random seed
    const char *csv_path; // where the per-second CSV goes
    int pattern;          // which access pattern (enum pattern)
    int hot_pct;          // hot patterns: % of accesses sent to the hot pages
    size_t seq_step;      // sequential pattern: bytes to advance per access
};

static void parse_args(int argc, char **argv, struct config *cfg) {
    static struct option opts[] = {
        {"size-mb",   required_argument, 0, 's'},
        {"seconds",   required_argument, 0, 't'},
        {"write-pct", required_argument, 0, 'w'},
        {"seed",      required_argument, 0, 'r'},
        {"csv",       required_argument, 0, 'c'},
        {"pattern",   required_argument, 0, 'p'},
        {"hot-pct",   required_argument, 0, 'h'},
        {"seq-step",  required_argument, 0, 'q'},
        {0, 0, 0, 0}};
    int o;
    while ((o = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        switch (o) {
        case 's': cfg->size_mb = strtoull(optarg, NULL, 10); break;
        case 't': cfg->seconds = atoi(optarg); break;
        case 'w': cfg->write_pct = atoi(optarg); break;
        case 'r': cfg->seed = strtoull(optarg, NULL, 10); break;
        case 'c': cfg->csv_path = optarg; break;
        case 'h': cfg->hot_pct = atoi(optarg); break;
        case 'q': cfg->seq_step = strtoull(optarg, NULL, 10); break;
        case 'p': {
            int found = -1;
            for (int i = 0; i < 4; i++)
                if (strcmp(optarg, PAT_NAME[i]) == 0) found = i;
            if (found < 0) { fprintf(stderr, "unknown pattern '%s'\n", optarg); exit(2); }
            cfg->pattern = found;
            break;
        }
        default:  exit(2);
        }
    }
}

// current time in seconds
static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// this process's CPU time so far, split into user and system, AND its page-fault count
static void cpu_snapshot(double *user_s, double *sys_s, long *faults) {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    *user_s = ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6;
    *sys_s = ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6;
    *faults = ru.ru_minflt;   // "minor" faults = faults that did not need disk I/O
}

static int cmp_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

// cheap, fast random number generator (xorshift64*)
static uint64_t rng_next(uint64_t *s) {
    uint64_t x = *s;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    *s = x;
    return x * 0x2545F4914F6CDD1DULL;
}
 
// Validity check: How much of this process the kernel currently backs with huge pages 
// this is a direct check that THP actually happened
static void print_anon_huge(const char *tag) {
    FILE *f = fopen("/proc/self/smaps_rollup", "r");
    if (!f) { printf("%s_anon_huge_mb=unavailable\n", tag); return; }
    char line[256];
    long kb = -1;
    while (fgets(line, sizeof line, f))
        sscanf(line, "AnonHugePages: %ld kB", &kb);   /* other lines don't match, so kb is left alone */
    fclose(f);
    if (kb < 0) printf("%s_anon_huge_mb=unavailable\n", tag);
    else printf("%s_anon_huge_mb=%.1f\n", tag, kb / 1024.0);
}

// Resident and swapped-out memory of this process, in MB (from smaps_rollup)
static void print_rss_swap(const char *tag) {
    FILE *f = fopen("/proc/self/smaps_rollup", "r");
    if (!f) return;
    char line[256];
    long rss = -1, swap = -1;
    while (fgets(line, sizeof line, f)) {
        sscanf(line, "Rss: %ld kB", &rss);
        sscanf(line, "Swap: %ld kB", &swap);
    }
    fclose(f);
    if (rss >= 0) printf("%s_rss_mb=%.1f\n", tag, rss / 1024.0);
    if (swap >= 0) printf("%s_swap_mb=%.1f\n", tag, swap / 1024.0);
}

// Background thread to collect per second data and save to a csv
static _Atomic uint64_t g_acc;   /* accesses done so far: the worker writes it, the reporter reads it */
static _Atomic int g_stop;       /* the reporter sets this to 1 when the time is up */
 
static double epoch_sec(void) {  /* wall-clock time, so CSV rows can be lined up with other logs */
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}
 
static double tv_to_s(struct timeval tv) { return tv.tv_sec + tv.tv_usec / 1e6; }
 
static long major_faults(void) { /* faults that needed disk I/O (e.g. a swap-in) */
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return ru.ru_majflt;
}
 
/* Wakes once per second, reads the worker's progress, writes one CSV row, and stops the
 * worker when --seconds have passed. It is a separate thread so that a worker stuck
 * waiting on the disk cannot silence the reporting. */
static void *reporter(void *arg) {
    const struct config *cfg = arg;
    FILE *out = NULL;
    if (cfg->csv_path) {
        out = fopen(cfg->csv_path, "w");
        if (!out) perror("csv");
        else fprintf(out, "epoch,t_s,acc_per_s,minflt,majflt,user_s,sys_s\n");
    }
    double t0 = now_sec(), prev_t = t0, next = t0 + 1.0;
    uint64_t prev_acc = 0;
    struct rusage prev;
    getrusage(RUSAGE_SELF, &prev);
    for (;;) {
        struct timespec ts = { (time_t)next, (long)((next - (time_t)next) * 1e9) };
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);   /* sleep until an exact time: no drift */
        double t = now_sec();
        uint64_t acc = atomic_load_explicit(&g_acc, memory_order_relaxed);
        struct rusage ru;
        getrusage(RUSAGE_SELF, &ru);
        if (out) {
            fprintf(out, "%.3f,%.1f,%.0f,%ld,%ld,%.3f,%.3f\n", epoch_sec(), t - t0,
                    (acc - prev_acc) / (t - prev_t),            /* accesses per second in this interval */
                    ru.ru_minflt - prev.ru_minflt, ru.ru_majflt - prev.ru_majflt,
                    tv_to_s(ru.ru_utime) - tv_to_s(prev.ru_utime),
                    tv_to_s(ru.ru_stime) - tv_to_s(prev.ru_stime));
            fflush(out);
        }
        prev = ru; prev_t = t; prev_acc = acc;
        if (t - t0 >= cfg->seconds) { atomic_store(&g_stop, 1); break; }
        next += 1.0;
    }
    if (out) fclose(out);
    return NULL;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
        struct config cfg = { .size_mb = 8192, .seconds = 30, .write_pct = 50,
                          .seed = 88172645463325252ULL, .csv_path = NULL,
                          .pattern = PAT_RANDOM, .hot_pct = 90, .seq_step = 64 };
    parse_args(argc, argv, &cfg);
    if (cfg.seq_step == 0 || cfg.seq_step % 8 != 0) { fprintf(stderr, "seq-step must be a positive multiple of 8\n"); return 1; }
    if (cfg.hot_pct < 0 || cfg.hot_pct > 100) { fprintf(stderr, "hot-pct must be 0 to 100\n"); return 1; }

    size_t size = (cfg.size_mb << 20) & ~(HUGE - 1);   // round down to a multiple of 2 MB
    if (size == 0) { fprintf(stderr, "size too small\n"); return 1; }
    size_t npages = size / PAGE;
    size_t nchunks = size / HUGE;

    // Reserve addresses (no real memory yet). Ask for 2 MB extra so the start can be aligned. */
    uint8_t *raw = mmap(NULL, size + HUGE, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (raw == MAP_FAILED) { perror("mmap"); return 1; }
    uint8_t *base = (uint8_t *)(((uintptr_t)raw + HUGE - 1) & ~(HUGE - 1));  // round UP to 2 MB 

    printf("size_mb=%zu\npages=%zu\nchunks_2MB=%zu\nseconds=%d\nwrite_pct=%d\n",
           size >> 20, npages, nchunks, cfg.seconds, cfg.write_pct);
    
    printf("pattern=%s\nhot_pct=%d\nseq_step=%zu\n", PAT_NAME[cfg.pattern], cfg.hot_pct, cfg.seq_step);

    // Hot patterns: a list of hot page numbers, one per 2 MB region (4096 pages at 8 GB).
    // hot-contig packs them into one contiguous block at the start of the array.
    // hot-scatter puts one at a random position inside each 2 MB region.
    size_t nhot = 0;
    uint32_t *hot = NULL;
    if (cfg.pattern == PAT_HOT_CONTIG || cfg.pattern == PAT_HOT_SCATTER) {
        nhot = nchunks;
        hot = malloc(nhot * sizeof *hot);
        if (!hot) { perror("malloc"); return 1; }
        uint64_t hs = cfg.seed ? (cfg.seed ^ 0x9E3779B97F4A7C15ULL) : 1;   // separate generator: access sequence unaffected
        for (size_t i = 0; i < nhot; i++)
            hot[i] = (cfg.pattern == PAT_HOT_CONTIG)
                         ? (uint32_t)i
                         : (uint32_t)(i * PAGES_PER_HUGE + rng_next(&hs) % PAGES_PER_HUGE);
        printf("hot_pages=%zu\nhot_set_mb=%.1f\n", nhot, nhot * PAGE / 1048576.0);
    }

    // ---- ALLOCATION STEP: touch one byte in every 4 KB page, timed per 2 MB chunk ---- */
    double *chunk_ms = malloc(nchunks * sizeof *chunk_ms);
    if (!chunk_ms) { perror("malloc"); return 1; }

    double u0, s0, u1, s1;
    long f0, f1;
    cpu_snapshot(&u0, &s0, &f0);              // CPU and fault counts BEFORE
    volatile uint8_t *p = base;
    printf("alloc_start_epoch=%.3f\n", epoch_sec());
    double t0 = now_sec();
    for (size_t c = 0; c < nchunks; c++) {
        double c0 = now_sec();
        for (size_t i = 0; i < PAGES_PER_HUGE; i++)
            p[(c * PAGES_PER_HUGE + i) * PAGE] = 1;   // first touch -> page fault
        chunk_ms[c] = (now_sec() - c0) * 1000.0;
    }
    double alloc_s = now_sec() - t0;
    cpu_snapshot(&u1, &s1, &f1);              // CPU and fault counts AFTER
    printf("alloc_end_epoch=%.3f\n", epoch_sec());

    qsort(chunk_ms, nchunks, sizeof *chunk_ms, cmp_double);   // sorted: median and max are easy
    printf("alloc_total_s=%.4f\n", alloc_s);
    printf("alloc_chunk_mean_ms=%.4f\n", alloc_s / nchunks * 1000.0);
    printf("alloc_chunk_median_ms=%.4f\n", chunk_ms[nchunks / 2]);
    printf("alloc_chunk_max_ms=%.4f\n", chunk_ms[nchunks - 1]);
    printf("alloc_user_s=%.4f\nalloc_sys_s=%.4f\nalloc_faults=%ld\n", u1 - u0, s1 - s0, f1 - f0);

    // ---- Validity check (make sure huge pages were actually used) ----
    print_rss_swap("after_alloc");
    print_anon_huge("after_alloc");        
 
    // ---- ACCESS STEP: random reads (and some writes) inside the same array ----
    uint64_t rng = cfg.seed;                 // same seed -> same access sequence in every run
    if (!rng) rng = 1;                      
    uint64_t n = 0;                          // accesses done so far 
    long maj0 = major_faults();
    pthread_t reporter_thread;               // start the reporter: it will stop us when --seconds are up
    if (pthread_create(&reporter_thread, NULL, reporter, &cfg)) { perror("pthread_create"); return 1; }
    cpu_snapshot(&u0, &s0, &f0);
    double start = now_sec();
    size_t seq_off = 0;
    while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
        uint64_t r = rng_next(&rng);
        size_t page, slot = (r >> 40) & 511;      // which 8-byte slot inside the page
        volatile uint64_t *addr;
        if (cfg.pattern == PAT_RANDOM) {
            page = r % npages;                    // exactly as in Experiment 1
            addr = (volatile uint64_t *)(base + page * PAGE + slot * 8);
        } else if (cfg.pattern == PAT_SEQ) {
            addr = (volatile uint64_t *)(base + seq_off);   // next position in order
            seq_off += cfg.seq_step;
            if (seq_off >= size) seq_off = 0;     // wrap to the start
        } else {                                  // hot-contig or hot-scatter: same code, different list
            uint64_t r2 = rng_next(&rng);
            if ((int)(r2 % 100) < cfg.hot_pct)
                page = hot[(r2 >> 32) % nhot];    // hot access
            else
                page = r % npages;                // access anywhere in the array
            addr = (volatile uint64_t *)(base + page * PAGE + slot * 8);
        }
        uint64_t v = *addr;                       // read
        if ((((r >> 52) * 100) >> 12) < (uint64_t)cfg.write_pct)
            *addr = v + 1;                        // write: this dirties the page
        n++;
        atomic_store_explicit(&g_acc, n, memory_order_relaxed);
    }
    double elapsed = now_sec() - start;
    pthread_join(reporter_thread, NULL);
    long maj1 = major_faults();
    cpu_snapshot(&u1, &s1, &f1);
 
    printf("access_total=%llu\n", (unsigned long long)n);
    printf("access_elapsed_s=%.3f\n", elapsed);
    printf("access_mean_per_s=%.0f\n", n / elapsed);
    printf("access_user_s=%.4f\naccess_sys_s=%.4f\naccess_faults=%ld\naccess_majflt=%ld\n",
           u1 - u0, s1 - s0, f1 - f0, maj1 - maj0);
    print_anon_huge("end");
    print_rss_swap("end");
    return 0;
}