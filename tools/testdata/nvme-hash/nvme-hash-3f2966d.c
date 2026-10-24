/* SPDX-License-Identifier: MIT */
/* RAM-only, read-only NVMe/SHA-256 helper for tools/nvme_hash.py.
 * Failure analysis: docs/A16-NVME-WRITES.md. Never builds a Write opcode.
 * The caller proves DMA ownership and supplies an idle M3-layout I/O queue.
 * Each invocation is bounded; queue cursors and partial progress are returned.
 */
#include <stdint.h>
#include <stddef.h>
#include "sha256.h"

#define MAGIC UINT64_C(0x31485341484d564e)
#define PAGE 4096u
#define ENTRIES 64u
#define DATA_OFF 0x200000u
#define PRP_OFF 0x8000u

struct work {
    uint64_t magic, bar, window, first_lba, blocks, lba_bytes, max_transfer, nsze;
    uint64_t tail, head, phase, next_cid, stride, io_entries, status, done_blocks;
    uint64_t last_completion, commands, overlay_table, overlay_count;
    struct q1n1_sha256 sha;
    uint8_t digest[32];
};
_Static_assert(sizeof(struct work) == 296, "Python ABI");

static inline uint64_t ticks(void)
{
    uint64_t v; __asm__ volatile("mrs %0, cntpct_el0" : "=r"(v)); return v;
}
static inline uint64_t frequency(void)
{
    uint64_t v; __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(v)); return v;
}
static inline void barrier(void) { __asm__ volatile("dsb sy" ::: "memory"); }
static void clean_invalidate(uint64_t address, uint64_t bytes)
{
    uint64_t ctr; __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    uint64_t line = UINT64_C(4) << ((ctr >> 16) & 15);
    uint64_t end = address + bytes;
    for (uint64_t p = address & ~(line - 1); p < end; p += line)
        __asm__ volatile("dc civac, %0" :: "r"(p) : "memory");
    barrier();
}
static uint32_t load32(uint64_t address) { return *(volatile uint32_t *)(uintptr_t)address; }
static void store32(uint64_t address, uint32_t value)
{
    *(volatile uint32_t *)(uintptr_t)address = value;
    barrier();
}

__attribute__((section(".text.entry")))
uint64_t nvme_hash_run(struct work *w, uint64_t reset, uint64_t final)
{
    w->status = 1;
    w->done_blocks = 0;
    if (w->magic != MAGIC || w->io_entries != ENTRIES || w->stride > 15 ||
        w->tail >= ENTRIES || w->head >= ENTRIES || w->phase > 1 ||
        !w->next_cid || w->next_cid > 65535 || !w->bar || (w->bar & 4095) ||
        !w->window || (w->window & 65535) ||
        (w->lba_bytes != 512 && w->lba_bytes != 4096) ||
        !w->max_transfer || w->max_transfer > 262144 ||
        w->max_transfer % w->lba_bytes || w->first_lba > w->nsze ||
        w->blocks > w->nsze - w->first_lba ||
        w->blocks > (UINT64_C(64) << 20) / w->lba_bytes || w->overlay_count > 256 ||
        (w->overlay_count && w->overlay_table != w->window + 0x61000))
        return w->status;
    if (reset) q1n1_sha256_init(&w->sha);
    uint64_t data = w->window + DATA_OFF;
    uint64_t sq_db = w->bar + 0x1000 + 2 * (UINT64_C(4) << w->stride);
    uint64_t cq_db = w->bar + 0x1000 + 3 * (UINT64_C(4) << w->stride);
    uint64_t per = w->max_transfer / w->lba_bytes;
    while (w->done_blocks < w->blocks) {
        uint64_t count = w->blocks - w->done_blocks;
        if (count > per) count = per;
        uint64_t size = count * w->lba_bytes;
        uint64_t pages = (size + PAGE - 1) / PAGE;
        uint64_t prp2 = 0;
        if (pages == 2) prp2 = data + PAGE;
        else if (pages > 2) {
            prp2 = w->window + PRP_OFF;
            volatile uint64_t *list = (volatile uint64_t *)(uintptr_t)prp2;
            for (uint64_t i = 1; i < pages; i++) list[i - 1] = data + PAGE * i;
            clean_invalidate(prp2, (pages - 1) * 8);
        }
        clean_invalidate(data, size);
        uint64_t sq = w->window + 0x2000 + w->tail * 64;
        volatile uint32_t *entry = (volatile uint32_t *)(uintptr_t)sq;
        for (unsigned i = 0; i < 16; i++) entry[i] = 0;
        uint32_t cid = (uint32_t)w->next_cid;
        uint64_t lba = w->first_lba + w->done_blocks;
        entry[0] = 0x02u | (cid << 16); /* NVM Read only. */
        entry[1] = 1;
        entry[6] = (uint32_t)data; entry[7] = (uint32_t)(data >> 32);
        entry[8] = (uint32_t)prp2; entry[9] = (uint32_t)(prp2 >> 32);
        entry[10] = (uint32_t)lba; entry[11] = (uint32_t)(lba >> 32);
        entry[12] = (uint32_t)(count - 1);
        clean_invalidate(sq, 64);
        w->tail = (w->tail + 1) % ENTRIES;
        w->next_cid = w->next_cid % 65535 + 1;
        store32(sq_db, (uint32_t)w->tail);
        uint64_t cq = w->window + 0x3000 + w->head * 16;
        uint64_t deadline = ticks() + frequency() * 5;
        uint32_t dw3;
        for (;;) {
            clean_invalidate(cq, 16);
            dw3 = load32(cq + 12);
            if (((dw3 >> 16) & 1) == w->phase) break;
            if (load32(w->bar + 0x1c) & 2) { w->status = 2; return 2; }
            if (ticks() > deadline) { w->status = 3; return 3; }
            __asm__ volatile("yield");
        }
        w->last_completion = dw3;
        if ((dw3 & 65535) != cid || (load32(cq + 8) >> 16) != 1) {
            w->status = 4; return 4;
        }
        w->head = (w->head + 1) % ENTRIES;
        if (!w->head) w->phase ^= 1;
        store32(cq_db, (uint32_t)w->head);
        if (dw3 >> 17) { w->status = 5; return 5; }
        clean_invalidate(data, size);
        /* Restore verification masks only reviewed disk ranges in the RAM
         * read buffer. The SSD is never written by this helper. */
        const uint64_t *overlays = (const uint64_t *)(uintptr_t)w->overlay_table;
        for (uint64_t i = 0; i < w->overlay_count; i++) {
            uint64_t disk_byte = overlays[i * 3], source = overlays[i * 3 + 1];
            uint64_t length = overlays[i * 3 + 2];
            if (!length || length > 0x100000 || source < w->window + 0x70000 ||
                source + length > w->window + 0x170000 ||
                disk_byte > w->nsze * w->lba_bytes || length > w->nsze * w->lba_bytes - disk_byte) {
                w->status = 6; return 6;
            }
            uint64_t begin = lba * w->lba_bytes, end = begin + size;
            uint64_t lo = begin > disk_byte ? begin : disk_byte;
            uint64_t hi = end < disk_byte + length ? end : disk_byte + length;
            if (lo < hi) {
                uint8_t *dest = (uint8_t *)(uintptr_t)(data + lo - begin);
                const uint8_t *src = (const uint8_t *)(uintptr_t)(source + lo - disk_byte);
                for (uint64_t j = 0; j < hi - lo; j++) dest[j] = src[j];
            }
        }
        q1n1_sha256_update(&w->sha, (const void *)(uintptr_t)data, size);
        w->done_blocks += count;
        w->commands++;
    }
    if (final) q1n1_sha256_final(&w->sha, w->digest);
    w->status = 0;
    return 0;
}
