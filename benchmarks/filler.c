/* filler.c - holds memory on one NUMA node so that other programs face memory pressure.
 * It allocates and locks memory (so it can never be swapped out) until the node's free memory
 * drops to --target-free-mb, prints READY, then sleeps until it is killed.
 * Build: gcc -O2 -Wall -o filler filler.c      Run it through numactl --membind=<node> */
#define _GNU_SOURCE
#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

/* Free memory of one NUMA node in MB, from /sys/devices/system/node/nodeN/meminfo. */
static long node_free_mb(int node) {
    char path[128], line[256];
    snprintf(path, sizeof path, "/sys/devices/system/node/node%d/meminfo", node);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    long kb = -1;
    while (fgets(line, sizeof line, f)) {
        char key[64];
        long v;
        if (sscanf(line, "Node %*d %63s %ld", key, &v) == 2 && strcmp(key, "MemFree:") == 0) kb = v;
    }
    fclose(f);
    return kb < 0 ? -1 : kb / 1024;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    int node = 0;
    long target_mb = -1;
    static struct option opts[] = {
        {"node", required_argument, 0, 'n'},
        {"target-free-mb", required_argument, 0, 't'},
        {0, 0, 0, 0}};
    int o;
    while ((o = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        if (o == 'n') node = atoi(optarg);
        else if (o == 't') target_mb = atol(optarg);
        else return 2;
    }
    if (target_mb < 256) { fprintf(stderr, "--target-free-mb must be at least 256\n"); return 2; }

    /* If memory ever runs out, the kernel should kill this program first. */
    FILE *adj = fopen("/proc/self/oom_score_adj", "w");
    if (adj) { fputs("1000", adj); fclose(adj); }

    long start_free = node_free_mb(node);
    if (start_free < 0) { fprintf(stderr, "cannot read node %d meminfo\n", node); return 1; }
    printf("start_free_mb=%ld\n", start_free);

    const size_t CHUNK = 256UL << 20;
    long held_mb = 0;
    for (;;) {
        long room_mb = node_free_mb(node) - target_mb;      /* how much more we may take */
        if (room_mb < 8) break;
        size_t room = (size_t)room_mb << 20;
        size_t len = (room < CHUNK ? room : CHUNK) & ~4095UL;
        void *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) { perror("mmap"); return 1; }
        madvise(p, len, MADV_NOHUGEPAGE);                    /* always 4 KB pages, whatever the THP setting */
        if (mlock(p, len) != 0) { perror("mlock"); return 1; }   /* fills the pages in and locks them */
        held_mb += (long)(len >> 20);
    }
    printf("held_mb=%ld\nnode_free_mb=%ld\nREADY\n", held_mb, node_free_mb(node));
    for (;;) pause();
}