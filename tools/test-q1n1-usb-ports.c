/* SPDX-License-Identifier: MIT */
/* Deterministic lifecycle model: real usb-ports.c, mocked controller drivers.
 * Covers two independent cables, stale configured flags, role reversal,
 * disconnect recovery, DMA ownership, and compatible stage handoff. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "usb-ports.h"

struct model {
    unsigned partner, dtr, bad_id, halt_failure, writes, suspended;
    unsigned device_starts, host_starts, opens, rebinds;
    uint32_t gctl, dctl, cmd, status;
    uint8_t input[32];
    unsigned head, tail;
    /* Q1N1_GADGET_NCM: the device-role NCM function and its UDP bytes. */
    unsigned ncm_up, announces, polls, acm_writes, net_writes;
    uint8_t net_input[32];
    unsigned net_head, net_tail;
} model[2];
static uint64_t now;
static void *arena;
static unsigned checks;
#define CHECK(x) do { assert(x); checks++; } while (0)

static unsigned index_of(uintptr_t base)
{
    assert(base == 0x0a600000 || base == 0x0a800000);
    return base == 0x0a800000;
}
uint64_t xhci_now_us(void) { return now; }
void qdwc3_delay_us(uint64_t us) { now += us; }
uint32_t qdwc3_rd(uintptr_t address)
{
    uintptr_t base = address & ~0x1fffffull;
    struct model *m = &model[index_of(base)];
    switch (address - base) {
    case 0: return 0x1200030;
    case 4: return 0x2000340;
    case 0x30: return m->cmd;
    case 0x34: return m->status;
    case 0xc110: return m->gctl;
    case 0xc120: return m->bad_id ? 0 : 0x33313130;
    case 0xc704: return m->dctl;
    case 0xc70c: return (m->dctl & (1u << 31)) ? m->suspended << 18 : (1u << 22);
    default: return 0;
    }
}
void qdwc3_wr(uintptr_t address, uint32_t value)
{
    uintptr_t base = address & ~0x1fffffull;
    struct model *m = &model[index_of(base)];
    m->writes++;
    switch (address - base) {
    case 0xc110: m->gctl = value; break;
    case 0xc704: m->dctl = value; break;
    case 0x30: m->cmd = value; if (!m->halt_failure) m->status = (value & 1) ? 0 : 1; break;
    default: break;
    }
}
int qdwc3_takeover(struct qdwc3 *d, uintptr_t base, uintptr_t qs,
                   const struct qdwc3_saved *saved, uint8_t *dma, uint64_t size)
{
    struct model *m = &model[index_of(base)];
    assert(((m->gctl >> 12) & 3) == 2);
    assert(size == QDWC3_ARENA_SIZE);
    assert(dma >= (uint8_t *)arena && dma + size <= (uint8_t *)arena + Q1N1_USB_ARENA_SIZE);
    memset(d, 0, sizeof(*d));
    d->regs = base; d->qscratch = qs; d->saved = saved; d->arena = dma;
    d->stats.init_step = 8;
    m->dctl = 1u << 31;
    m->device_starts++;
    return 0;
}
void qdwc3_poll(struct qdwc3 *d)
{
    struct model *m = &model[index_of(d->regs)];
    m->polls++;
    if (m->partner == Q1N1_USB_HOST) d->stats.configured = 1;
    /* Deliberately leave configured latched when unplugged. */
}
int qdwc3_link_down(const struct qdwc3 *d)
{ return model[index_of(d->regs)].partner != Q1N1_USB_HOST; }
int qdwc3_ready(const struct qdwc3 *d, unsigned pipe)
{ return !pipe && d->stats.configured && model[index_of(d->regs)].dtr; }
void qdwc3_stop(struct qdwc3 *d) { model[index_of(d->regs)].dctl = 0; }
static size_t input(unsigned i, uint8_t *buffer, size_t count)
{
    struct model *m = &model[i];
    size_t got = 0;
    while (got < count && m->tail < m->head) buffer[got++] = m->input[m->tail++];
    return got;
}
size_t qdwc3_read(struct qdwc3 *d, unsigned pipe, uint8_t *b, size_t n)
{ return pipe ? 0 : input(index_of(d->regs), b, n); }
size_t qdwc3_write(struct qdwc3 *d, unsigned pipe, const uint8_t *b, size_t n)
{ (void)pipe; (void)b; model[index_of(d->regs)].acm_writes++; return n; }
int xhci_init(struct xhci *x, uintptr_t base, uint8_t *dma, uint64_t size)
{
    struct model *m = &model[index_of(base)];
    assert(((m->gctl >> 12) & 3) == 1);
    assert(dma >= (uint8_t *)arena && dma + size <= (uint8_t *)arena + Q1N1_USB_ARENA_SIZE);
    memset(x, 0, sizeof(*x));
    x->base = base; x->max_ports = 2; x->dma = dma; x->stats.init_step = 4;
    m->status = 0; m->cmd = 1; m->host_starts++;
    return 0;
}
uint32_t xhci_portsc(struct xhci *x, uint32_t port)
{ return port == 1 && model[index_of(x->base)].partner == Q1N1_USB_DEVICE ? 0xe03 : 0x2a0; }
int xhci_running(struct xhci *x) { return model[index_of(x->base)].cmd & 1; }
int ncm_open(struct ncm *n, struct xhci *x, uint32_t which, uint32_t wait)
{
    (void)which; (void)wait;
    memset(n, 0, sizeof(*n)); n->x = x; n->stats.init_step = 9;
    n->device.port = 1; n->device.slot = 1;
    model[index_of(x->base)].opens++;
    return 0;
}
void ncm_close(struct ncm *n) { n->x = NULL; }
/* Which port a link belongs to: its xHCI, or (device role) its DWC3. */
static unsigned link_index(const struct ncm *n)
{
#ifdef Q1N1_GADGET_NCM
    if (!n->x) return index_of(n->gadget->regs);
#endif
    return index_of(n->x->base);
}
static int net_ready(void *ctx) { return ((struct ncm_proxy *)ctx)->have_peer; }
static void net_poll(void *ctx) { (void)ctx; }
static size_t net_read(void *ctx, uint8_t *b, size_t n)
{
    const struct ncm *link = ((struct ncm_proxy *)ctx)->n;
#ifdef Q1N1_GADGET_NCM
    if (!link->x) {
        struct model *m = &model[link_index(link)];
        size_t got = 0;
        while (got < n && m->net_tail < m->net_head) b[got++] = m->net_input[m->net_tail++];
        return got;
    }
#endif
    return input(link_index(link), b, n);
}
static size_t net_write(void *ctx, const uint8_t *b, size_t n)
{ (void)b; model[link_index(((struct ncm_proxy *)ctx)->n)].net_writes++; return n; }
static uint8_t *batch_at[2], *side_at[2];
static uint32_t batch_size[2], side_size[2];
void ncm_attach_batch(struct ncm *n, uint8_t *buffer, uint32_t size)
{ batch_at[index_of(n->x->base)] = buffer; batch_size[index_of(n->x->base)] = size; }
void ncm_attach_side(struct ncm *n, uint8_t *buffer, uint32_t size)
{ side_at[index_of(n->x->base)] = buffer; side_size[index_of(n->x->base)] = size; }
/* The side buffer (sends from inside a P_CALL) is its own memory: after the
 * batch, inside the port's slot of the arena, clear of every DMA region. */
static int side_clear(const struct q1n1_usb_ports *s, unsigned i)
{
    const uint8_t *slot = (const uint8_t *)arena + i * (2u << 20);
    return side_at[i] && side_size[i] == NCM_SIDE_BYTES && batch_at[i] &&
           side_at[i] >= batch_at[i] + batch_size[i] && side_at[i] + side_size[i] <= slot + (2u << 20) &&
           side_at[i] >= s->port[i].host_dma + Q1N1_USB_HOST_ARENA;
}
void ncm_proxy_rebind(struct ncm_proxy *p, struct ncm *n)
{
    p->n = n;
    p->io = (struct q1n1_io){.read = net_read, .write = net_write, .ready = net_ready,
                            .poll = net_poll, .ctx = p};
    model[link_index(n)].rebinds++;
}
void ncm_proxy_init(struct ncm_proxy *p, struct ncm *n, uint64_t timeout)
{ (void)timeout; memset(p, 0, sizeof(*p)); ncm_proxy_rebind(p, n); p->have_peer = 1; }
void ncm_proxy_announce(struct ncm_proxy *p) { model[link_index(p->n)].announces++; }
static unsigned call_polls[2];
void ncm_proxy_call_poll(struct ncm_proxy *p) { call_polls[link_index(p->n)]++; }
#ifdef Q1N1_GADGET_NCM
void ncm_open_gadget(struct ncm *n, struct qdwc3 *d)
{
    memset(n, 0, sizeof(*n));
    n->gadget = d;
    n->stats.init_step = 9;
}
int ncm_gadget_up(const struct ncm *n)
{ return n->gadget && n->gadget->stats.configured && model[index_of(n->gadget->regs)].ncm_up; }
#endif

static struct q1n1_usb_ports *reset(unsigned a, unsigned b)
{
    memset(arena, 0, Q1N1_USB_ARENA_SIZE); memset(model, 0, sizeof(model)); now = 1;
    for (unsigned i = 0; i < 2; i++) {
        model[i].gctl = 0x102001; model[i].status = 1; model[i].dtr = 1;
    }
    model[0].partner = a; model[1].partner = b;
    return q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 0);
}
static void advance(struct q1n1_usb_ports *s, uint64_t us)
{ now += us; q1n1_usb_ports_poll(s); }
static void settle(struct q1n1_usb_ports *s)
{ for (unsigned n = 0; n < 50; n++) advance(s, 100000); }


#ifdef Q1N1_GADGET_NCM
/* A device-role port also serves UDP over its NCM function: both transports
 * work, a request stays on the one it started on, each negotiates its own
 * checksums, a P_CALL services it, and it re-announces when reopened. */
static void gadget_checks(void)
{
    struct q1n1_usb_ports *s = reset(Q1N1_USB_DEVICE, Q1N1_USB_HOST);
    settle(s);
    CHECK(s->status[1].role == Q1N1_USB_DEVICE && s->status[1].connected);
    CHECK(s->port[1].ncm.gadget == &s->port[1].device && s->port[1].ncm.x == NULL);
    CHECK(s->port[1].proxy.n == &s->port[1].ncm && model[1].announces == 0);
    model[1].ncm_up = 1;
    advance(s, 1000);
    CHECK(model[1].announces == 1);
    advance(s, 1000);
    CHECK(model[1].announces == 1);                  /* once per opening */
    /* NCM bytes first: the request is read and answered on NCM only. */
    model[1].net_input[0] = 0x55; model[1].net_input[1] = 0x66; model[1].net_head = 2;
    model[1].input[0] = 0x77; model[1].head = 1;
    model[0].head = model[0].tail = 0;
    uint8_t byte;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x77);   /* ACM is looked at first... */
    s->io.end(s);
    unsigned acm = model[1].acm_writes, net = model[1].net_writes;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x55);   /* ...then NCM */
    *s->io.checksum_state(s) = 1;
    model[1].input[1] = 0x78; model[1].head = 2;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x66);   /* not spliced with ACM bytes */
    CHECK(s->io.read(s, &byte, 1) == 0);
    CHECK(s->io.write(s, &byte, 1) == 1 && model[1].net_writes == net + 1 && model[1].acm_writes == acm);
    s->io.end(s);
    CHECK(s->port[1].gadget_checksum_state == 1 && s->port[1].checksum_state == 0);
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x78 && *s->io.checksum_state(s) == 0);
    CHECK(s->io.write(s, &byte, 1) == 1 && model[1].acm_writes == acm + 1);
    s->io.end(s);
    /* Inside a P_CALL the device-role link is serviced too: events and UDP. */
    unsigned polls = model[1].polls, starts = model[1].device_starts;
    call_polls[0] = call_polls[1] = 0;
    for (unsigned n = 0; n < 3; n++) { now += 1000000; q1n1_usb_ports_call_poll(s); }
    CHECK(call_polls[1] == 3 && call_polls[0] == 3 && model[1].polls == polls + 3);
    CHECK(model[1].device_starts == starts && s->status[1].role == Q1N1_USB_DEVICE);
    model[1].ncm_up = 0;
    q1n1_usb_ports_call_poll(s);
    CHECK(call_polls[1] == 3 && model[1].polls == polls + 4);   /* events still, no UDP */
    /* Closed and reopened by the host: announced again. */
    advance(s, 1000);
    model[1].ncm_up = 1;
    advance(s, 1000);
    CHECK(model[1].announces == 2);
    /* A port that is not ready on either transport is not read. */
    model[1].dtr = 0; model[1].ncm_up = 0;
    model[1].net_head = model[1].net_tail = 0;
    model[1].input[0] = 0x99; model[1].head = 1; model[1].tail = 0;
    CHECK(s->io.read(s, &byte, 1) == 0);
    model[1].dtr = 1;
    /* Unplugged: the role is released and the gadget link with it. */
    model[1].partner = 0;
    advance(s, 1); advance(s, 600000);
    CHECK(s->status[1].role == Q1N1_USB_OFF && s->port[1].ncm.gadget == NULL);
    CHECK(s->status[0].connected);
    /* The layout contract says this is not the default build's. */
    extern const uint64_t q1n1_usb_ports_contract[3];
    CHECK(q1n1_usb_ports_contract[1] == 2 && q1n1_usb_ports_contract[2] == sizeof(struct q1n1_usb_ports));
}
#endif

int main(void)
{
    assert(!posix_memalign(&arena, 0x10000, Q1N1_USB_ARENA_SIZE));
    CHECK(q1n1_usb_ports_init((uint8_t *)arena + 1, Q1N1_USB_ARENA_SIZE, 0) == NULL);
    struct q1n1_usb_ports *s = reset(Q1N1_USB_DEVICE, Q1N1_USB_HOST);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_HOST && s->status[0].connected);
    CHECK(s->status[1].role == Q1N1_USB_DEVICE && s->status[1].connected);
    CHECK(model[1].device_starts == 1);
    CHECK(s->port[0].host_dma + Q1N1_USB_HOST_ARENA <= s->port[1].device_dma);
    CHECK(side_clear(s, 0));
    model[0].input[0] = 0x11; model[0].input[1] = 0x22; model[0].head = 2;
    model[1].input[0] = 0x33; model[1].input[1] = 0x44; model[1].head = 2;
    uint8_t byte;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x11);
    *s->io.checksum_state(s) = 1;
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x22);
    CHECK(s->io.read(s, &byte, 1) == 0); /* Cannot splice the other request. */
    s->io.end(s);
    CHECK(s->io.read(s, &byte, 1) == 1 && byte == 0x33);
    CHECK(*s->io.checksum_state(s) == 0 && s->port[0].checksum_state == 1);
    s->io.end(s);
    unsigned starts = model[1].device_starts;
    model[1].dtr = 0;
    advance(s, 31000000);
    CHECK(s->status[1].role == Q1N1_USB_DEVICE && model[1].device_starts == starts);
    model[1].dtr = 1;
    model[0].partner = 0;
    advance(s, 1); advance(s, 600000);
    CHECK(s->status[0].role == Q1N1_USB_OFF && s->port[0].ncm.x == NULL);
    CHECK(s->status[0].disconnects == 1 && model[1].device_starts == starts);
    model[0].partner = Q1N1_USB_DEVICE;
    settle(s);
    CHECK(s->status[0].connected && model[0].opens == 2);
    unsigned hosts = model[0].host_starts;
    q1n1_usb_ports_handoff(s);
    side_at[0] = batch_at[0] = NULL;
    s = q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 1);
    CHECK(model[0].host_starts == hosts && s->status[0].connected);
    CHECK(side_clear(s, 0));                         /* an adopted link gets one too */
    CHECK(s->port[0].proxy.io.ctx == &s->port[0].proxy);
    settle(s);
    CHECK(s->status[1].connected);
    model[1].partner = 0;
    advance(s, 1); advance(s, 600000);
    CHECK(s->status[1].role == Q1N1_USB_OFF && s->status[1].disconnects == 1);
    model[1].partner = Q1N1_USB_DEVICE;
    settle(s);
    CHECK(s->status[1].role == Q1N1_USB_HOST && s->status[1].connected);
    CHECK(model[0].host_starts == hosts);
    s = reset(Q1N1_USB_HOST, Q1N1_USB_DEVICE);
    settle(s);
    starts = model[0].device_starts;
    model[0].suspended = 3;
    advance(s, 1); advance(s, 1000000);
    CHECK(s->status[0].connected && model[0].device_starts == starts);
    advance(s, 8000000); advance(s, 600000);
    CHECK(s->status[0].role == Q1N1_USB_OFF && s->status[0].disconnects == 1);
    CHECK(s->status[1].connected && model[1].host_starts == 1);
    s = reset(Q1N1_USB_HOST, Q1N1_USB_DEVICE);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_DEVICE && s->status[0].connected);
    CHECK(s->status[1].role == Q1N1_USB_HOST && s->status[1].connected);
    /* An idle port does not rediscover while the other one carries traffic:
     * each attempt stops the loop for 50-100 ms. It resumes once traffic stops. */
    s = reset(0, Q1N1_USB_HOST);
    settle(s);
    CHECK(s->status[1].connected && !s->status[0].connected);
    unsigned probes = model[0].device_starts + model[0].host_starts;
    for (unsigned n = 0; n < 100; n++) {
        model[1].input[0] = (uint8_t)n; model[1].head = 1; model[1].tail = 0;
        CHECK(s->io.read(s, &byte, 1) == 1 && byte == (uint8_t)n);
        s->io.end(s);
        advance(s, 100000);
    }
    CHECK(model[0].device_starts + model[0].host_starts == probes);
    for (unsigned n = 0; n < 60; n++) advance(s, 100000);
    CHECK(model[0].device_starts + model[0].host_starts > probes);
    CHECK(s->status[1].connected);
    s = reset(0, Q1N1_USB_HOST);
    model[0].bad_id = 1;
    s = q1n1_usb_ports_init(arena, Q1N1_USB_ARENA_SIZE, 0);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_UNSUPPORTED && !model[0].writes);
    CHECK(s->status[1].connected);
    s = reset(0, Q1N1_USB_HOST);
    model[0].halt_failure = 1; model[0].status = 0;
    settle(s);
    CHECK(model[0].gctl == 0x102001 && model[0].device_starts == 0);
    CHECK(s->status[1].connected);
    /* From inside a P_CALL: only configured NCM links are serviced, and
     * nothing is started, stopped or rediscovered underneath the call. */
    s = reset(Q1N1_USB_DEVICE, Q1N1_USB_HOST);
    settle(s);
    CHECK(s->status[0].role == Q1N1_USB_HOST && s->status[1].role == Q1N1_USB_DEVICE);
    unsigned host_starts = model[0].host_starts, device_starts = model[1].device_starts;
    unsigned opens = model[0].opens;
    for (unsigned n = 0; n < 5; n++) { now += 10000000; q1n1_usb_ports_call_poll(s); }
    CHECK(call_polls[0] == 5 && call_polls[1] == 0);
    CHECK(model[0].host_starts == host_starts && model[1].device_starts == device_starts &&
          model[0].opens == opens && s->status[0].connected && s->status[1].connected);
    s->busy = 1;
    q1n1_usb_ports_call_poll(s);
    CHECK(call_polls[0] == 5);                       /* the loop owns the driver */
    s->busy = 0;
    model[0].partner = 0;                            /* cable gone, not yet noticed */
    q1n1_usb_ports_call_poll(s);
    CHECK(call_polls[0] == 5 && s->status[0].role == Q1N1_USB_HOST && s->status[0].connected);
    q1n1_usb_ports_call_poll(NULL);
#ifdef Q1N1_GADGET_NCM
    gadget_checks();
#endif
    free(arena);
    printf("PASS %u independent USB-port lifecycle checks\n", checks);
    return 0;
}
