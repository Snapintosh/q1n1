/* SPDX-License-Identifier: MIT */
/* Each powered A16 controller discovers its own USB data direction. GCTL is
 * local controller configuration, not a Type-C contract. No PD commands or
 * clock/PHY power guesses are needed: alternate bounded host/device attempts
 * while idle, and never disturb another port or a configured connection. */
#include "usb-ports.h"
void *memset(void *dest, int value, size_t n);

#define PORTS_MAGIC UINT64_C(0x325354524f505355)
#ifdef Q1N1_GADGET_NCM
#define PORTS_VERSION 2   /* struct ncm, qdwc3 and the port grew; never adopt across builds */
#else
#define PORTS_VERSION 1
#endif
#define GCTL 0xc110
#define GSNPSID 0xc120
#define DCTL 0xc704
#define DSTS 0xc70c
#define RUN_STOP (1u << 31)
#define ROLE_MASK (3u << 12)
#define HOST_DWELL_US 1500000u
#define DEVICE_DWELL_US 3000000u
#define DISCONNECT_US 500000u
#define SUSPEND_RECOVERY_US 8000000u
#define HOST_SLOT_BYTES (2u << 20)

/* Referenced by the stage header so the reader can reject an update that
 * would reset a live NCM link instead of adopting its shared state. */
const uint64_t q1n1_usb_ports_contract[3] = {
    PORTS_MAGIC, PORTS_VERSION, sizeof(struct q1n1_usb_ports)
};

/* The rest of each port's slot, after its xHCI arena, stages batched NCM
 * transmits. Nothing else uses it, and it is the same UEFI-allocated memory the
 * controllers already DMA from. */
#define HOST_BATCH_OFFSET (0x10000 + QDWC3_ARENA_SIZE + Q1N1_USB_HOST_ARENA)
/* After the batch, the one-frame buffer for sends from inside a P_CALL
 * (ncm_send_side), which must not share memory with anything in flight. */
#define HOST_SIDE_OFFSET (HOST_BATCH_OFFSET + NCM_BATCH_BYTES)

/* Rediscovery on an idle port stops the poll loop for 50-100 ms: a role change
 * settles for 50 ms and a device takeover waits another 50. Measured over NCM
 * that was a 103 ms and a 53 ms stall every 4.9 s, and a host write burst that
 * lands in one outruns the Mac's queue. So an idle port waits while another
 * port is carrying proxy traffic. Per stage, not shared state: a new stage
 * starts with no traffic seen, which only allows one early probe. */
#define TRAFFIC_QUIET_US 2000000u
static uint64_t last_traffic_us;

_Static_assert(sizeof(struct q1n1_usb_ports) < 0x10000, "USB state fits before DMA");
_Static_assert(HOST_BATCH_OFFSET + NCM_BATCH_BYTES <= HOST_SLOT_BYTES,
               "USB port DMA regions do not overlap");
_Static_assert(HOST_SIDE_OFFSET + NCM_SIDE_BYTES <= HOST_SLOT_BYTES,
               "USB port side buffer fits its slot");

static int host_present(const struct q1n1_usb_port *p)
{
    return p->ncm.x && p->ncm.stats.init_step == 9 && p->ncm.device.port &&
           (xhci_portsc((struct xhci *)&p->host, p->ncm.device.port) & 3u) == 3u;
}

#ifdef Q1N1_GADGET_NCM
/* Which transport of a device-role port the request being read arrived on:
 * its ACM pipe or UDP over its NCM function. Like `selected` for ports, it
 * keeps one request on one transport until io_end. Per stage. */
enum { VIA_ANY, VIA_ACM, VIA_NCM };
static uint32_t via[Q1N1_USB_PORTS];

static int gadget_ready(struct q1n1_usb_port *p)
{
    return ncm_gadget_up(&p->ncm) && p->proxy.io.ready(p->proxy.io.ctx);
}

/* How long a live device-role link went unserviced. Whatever holds the loop
 * (a role change on the other port, a long call) shows up here, and while it
 * lasts the Mac's NCM queue only grows: it drops silently once full. */
static uint64_t serviced_us[Q1N1_USB_PORTS];
static void note_service(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    uint64_t now = xhci_now_us();
    if (s->status[i].role != Q1N1_USB_DEVICE || !ncm_gadget_up(&p->ncm)) {
        serviced_us[i] = 0;
        return;
    }
    if (serviced_us[i]) {
        struct qdwc3_ncm_stats *stats = &p->device.stats.ncm;
        uint64_t gap = now - serviced_us[i];
        if (gap > stats->service_gap_max_us) stats->service_gap_max_us = gap;
        if (gap > 20000) stats->service_gaps_20ms++;
    }
    serviced_us[i] = now;
}

/* Do not stall a live link for discovery. A role attempt on an idle port holds
 * the whole loop for 50-150 ms (role settle, controller resets), and the
 * default rule only avoids that while traffic is under 2 s old, so a write
 * that starts after a pause can land in one: that is how a host-role link lost
 * writes before (A16-Q1N1-PROXY.md, transfer speed), and a gadget link is no
 * different. So the default rule holds only for a stage's first 30 s, which is
 * when cables already plugged in are found; after that an idle port waits
 * while any other port is connected, and a cable plugged in later is found
 * once the live link goes away. */
#define DISCOVERY_WINDOW_US 30000000u
static uint64_t ports_started_us;
static uint32_t deferring[Q1N1_USB_PORTS];
static int other_port_live(struct q1n1_usb_ports *s, unsigned i)
{
    int live = 0;
    for (unsigned k = 0; k < Q1N1_USB_PORTS; k++) {
        if (k == i || !s->status[k].connected) continue;
        live = 1;
        if (!deferring[i] && s->status[k].role == Q1N1_USB_DEVICE)
            s->port[k].device.stats.ncm.probes_deferred++;
    }
    deferring[i] = (uint32_t)live;
    return live;
}
#endif

static int port_ready(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
#ifdef Q1N1_GADGET_NCM
    if (s->status[i].role == Q1N1_USB_DEVICE)
        return !qdwc3_link_down(&p->device) && (qdwc3_ready(&p->device, 0) || gadget_ready(p));
#else
    if (s->status[i].role == Q1N1_USB_DEVICE)
        return !qdwc3_link_down(&p->device) && qdwc3_ready(&p->device, 0);
#endif
    if (s->status[i].role == Q1N1_USB_HOST)
        return host_present(p) && p->proxy.io.ready(p->proxy.io.ctx);
    return 0;
}

static int halt_host(struct q1n1_usb_port *p)
{
    /* Do not change direction while xHCI might still own DMA. */
    uintptr_t op = p->base + (qdwc3_rd(p->base) & 0xff);
    qdwc3_wr(op, qdwc3_rd(op) & ~1u);
    for (unsigned n = 0; n < 1000; n++) {
        if (qdwc3_rd(op + 4) & 1u) return 0;
        qdwc3_delay_us(100);
    }
    return -1;
}

static int stop_port(struct q1n1_usb_ports *s, unsigned i, int graceful)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    if (st->role == Q1N1_USB_DEVICE) {
        qdwc3_stop(&p->device);
        if (qdwc3_rd(p->base + DCTL) & RUN_STOP) return -1;
        if (!(qdwc3_rd(p->base + DSTS) & (1u << 22))) return -1;
        qdwc3_wr(p->qscratch + 0x10, p->qs_hs);
        qdwc3_wr(p->qscratch + 0x30, p->qs_ss);
    } else if (st->role == Q1N1_USB_HOST) {
        if (graceful && host_present(p)) ncm_close(&p->ncm);
        if (halt_host(p)) return -1;
    }
    p->ncm.x = NULL;
#ifdef Q1N1_GADGET_NCM
    p->ncm.gadget = NULL;            /* the DWC3 stop above ended its transfers */
    p->gadget_announced = 0;
    p->gadget_checksum_state = 0;
    via[i] = VIA_ANY;
#endif
    p->proxy.have_peer = 0;
    p->proxy.head = p->proxy.tail = 0;
    st->connected = 0;
    st->role = Q1N1_USB_OFF;
    p->down_since = 0;
    p->suspended_since = 0;
    p->console_ready = 0;
    p->checksum_state = 0;
    return 0;
}

static int snapshot(struct q1n1_usb_port *p)
{
    if (qdwc3_rd(p->base + GSNPSID) != 0x33313130 ||
        (qdwc3_rd(p->base) & 0xff) != 0x30 ||
        (qdwc3_rd(p->base + 4) >> 24) != 2) return -1;
    p->saved = (struct qdwc3_saved){
        .gsbuscfg0 = qdwc3_rd(p->base + 0xc100), .gsbuscfg1 = qdwc3_rd(p->base + 0xc104),
        .gctl = qdwc3_rd(p->base + GCTL), .guctl = qdwc3_rd(p->base + 0xc12c),
        .guctl1 = qdwc3_rd(p->base + 0xc11c), .guctl2 = qdwc3_rd(p->base + 0xc19c),
        .gusb2phycfg = qdwc3_rd(p->base + 0xc200), .gusb3pipectl = qdwc3_rd(p->base + 0xc2c0),
        .gfladj = qdwc3_rd(p->base + 0xc630), .dcfg = qdwc3_rd(p->base + 0xc700),
        .gsnpsid = qdwc3_rd(p->base + GSNPSID)};
    p->qs_hs = qdwc3_rd(p->qscratch + 0x10);
    p->qs_ss = qdwc3_rd(p->qscratch + 0x30);
    return 0;
}

static int start_port(struct q1n1_usb_ports *s, unsigned i, unsigned role)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    st->attempts++;
    /* Firmware/previous stages have stopped their engines before this point.
     * Still halt the host explicitly before changing direction. Device takeover
     * also quiesces and resets its engine before publishing new DMA pointers. */
    if (halt_host(p)) return -10;
    if (role == Q1N1_USB_HOST && (qdwc3_rd(p->base + DCTL) & RUN_STOP)) return -11;
    qdwc3_wr(p->base + GCTL, (p->saved.gctl & ~ROLE_MASK) | (role << 12));
    qdwc3_delay_us(50000);
    st->role = role;
    int result;
    if (role == Q1N1_USB_HOST) {
        result = xhci_init(&p->host, p->base, p->host_dma, Q1N1_USB_HOST_ARENA);
        p->deadline = xhci_now_us() + HOST_DWELL_US;
    } else {
        result = qdwc3_takeover(&p->device, p->base, p->qscratch, &p->saved,
                               p->device_dma, QDWC3_ARENA_SIZE);
        p->deadline = xhci_now_us() + DEVICE_DWELL_US;
#ifdef Q1N1_GADGET_NCM
        /* The same UDP transport as the host role, over this function. It
         * carries nothing until the Mac selects the data interface. */
        if (!result) {
            ncm_open_gadget(&p->ncm, &p->device);
            ncm_proxy_init(&p->proxy, &p->ncm, 0);
            p->gadget_announced = 0;
            p->gadget_checksum_state = 0;
        }
#endif
    }
    p->started = xhci_now_us();
    p->activity = 0;
    p->down_since = 0;
    return result;
}

static void failed(struct q1n1_usb_ports *s, unsigned i, int result)
{
    struct q1n1_usb_port *p = &s->port[i];
    s->status[i].last_error = (uint64_t)(int64_t)result;
    if (stop_port(s, i, 0)) {
        s->status[i].role = Q1N1_USB_UNSUPPORTED; /* Cannot safely reuse DMA. */
        return;
    }
    if (p->failures < 4) p->failures++;
    p->retry_at = xhci_now_us() + (250000u << p->failures);
}

static void service_port(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    if (s->status[i].role == Q1N1_USB_DEVICE) {
#ifdef Q1N1_GADGET_NCM
        note_service(s, i);
#endif
        qdwc3_poll(&p->device);
#ifdef Q1N1_GADGET_NCM
        /* Interfaces 2/3 are the NCM function in this build, not the banner
         * console. Announce once per opening of the data interface, so the
         * Mac's neighbour cache is seeded before its first datagram. */
        if (ncm_gadget_up(&p->ncm)) {
            if (!p->gadget_announced) {
                ncm_proxy_announce(&p->proxy);
                p->gadget_announced = 1;
            }
            p->proxy.io.poll(p->proxy.io.ctx);
        } else {
            p->gadget_announced = 0;
        }
#else
        uint8_t discard[64];
        qdwc3_read(&p->device, 1, discard, sizeof(discard));
        int ready = qdwc3_ready(&p->device, 1);
        if (ready && !p->console_ready) {
            static const uint8_t banner[] = "q1n1 EL2: proxy is on the first CDC port.\r\n";
            qdwc3_write(&p->device, 1, banner, sizeof(banner) - 1);
        }
        p->console_ready = (uint32_t)ready;
#endif
    } else if (s->status[i].role == Q1N1_USB_HOST && host_present(p)) {
        p->proxy.io.poll(p->proxy.io.ctx);
    }
}

static int other_port_busy(const struct q1n1_usb_ports *s, unsigned i, uint64_t now)
{
    if (!last_traffic_us || now - last_traffic_us >= TRAFFIC_QUIET_US) return 0;
    for (unsigned k = 0; k < Q1N1_USB_PORTS; k++)
        if (k != i && s->status[k].connected) return 1;
    return 0;
}

static void maintain_port(struct q1n1_usb_ports *s, unsigned i)
{
    struct q1n1_usb_port *p = &s->port[i];
    struct q1n1_usb_port_status *st = &s->status[i];
    uint64_t now = xhci_now_us();
    if (st->role == Q1N1_USB_UNSUPPORTED) return;
#ifdef Q1N1_GADGET_NCM
    /* Only discovery waits: a port whose own link was up and has just dropped
     * still goes through its disconnect handling below. */
    int established = (st->role == Q1N1_USB_HOST && p->ncm.x && p->ncm.stats.init_step == 9) ||
                      (st->role == Q1N1_USB_DEVICE && p->device.stats.configured);
    if (!st->connected && !established &&
        (now - ports_started_us < DISCOVERY_WINDOW_US ? other_port_busy(s, i, now) : other_port_live(s, i)))
        return;
#else
    if (!st->connected && other_port_busy(s, i, now)) return;
#endif
    if (st->role == Q1N1_USB_OFF) {
        if (now < p->retry_at) return;
        unsigned role = p->next_role;
        p->next_role = role == Q1N1_USB_HOST ? Q1N1_USB_DEVICE : Q1N1_USB_HOST;
        int result = start_port(s, i, role);
        if (result) failed(s, i, result);
        return;
    }
    if (st->role == Q1N1_USB_DEVICE) {
        uint64_t activity = p->device.stats.setups + p->device.stats.resets;
        int down = qdwc3_link_down(&p->device);
        /* The A16 forces session-valid. A real cable removal can therefore
         * leave USB2 in U3/Suspend, with configured and DTR still latched,
         * instead of producing Disconnected. Allow ordinary short suspends;
         * after sustained bus silence release the stale role and rediscover.
         * A genuinely sleeping host may re-enumerate after this timeout. */
        unsigned link_state = (qdwc3_rd(p->base + DSTS) >> 18) & 15u;
        if (link_state == 3 && p->device.stats.configured) {
            if (!p->suspended_since) p->suspended_since = now;
            if (now - p->suspended_since >= SUSPEND_RECOVERY_US) down = 1;
        } else {
            p->suspended_since = 0;
        }
        st->connected = p->device.stats.configured && !down;
        if (p->device.stats.configured) {
            if (!down) { p->down_since = 0; p->failures = 0; return; }
            if (!p->down_since) p->down_since = now;
            if (now - p->down_since < DISCONNECT_US) return;
            st->disconnects++;
        } else {
            /* Do not kill a first enumeration merely because the link starts
             * disconnected. Setup/reset activity earns time, bounded overall. */
            if (activity != p->activity) {
                p->deadline = now + 8000000u;
                p->activity = activity;
            }
            if (now < p->deadline && now - p->started < 30000000u) return;
        }
        if (stop_port(s, i, 0)) { failed(s, i, -12); return; }
        p->next_role = Q1N1_USB_HOST;
        p->retry_at = now + 100000u;
        return;
    }
    if (p->ncm.stats.init_step == 9 && p->ncm.x) {
        st->connected = host_present(p);
        if (st->connected) { p->down_since = 0; p->failures = 0; return; }
        if (!p->down_since) p->down_since = now;
        if (now - p->down_since < DISCONNECT_US) return;
        st->disconnects++;
        if (stop_port(s, i, 0)) { failed(s, i, -13); return; }
        p->next_role = Q1N1_USB_HOST;
        p->retry_at = now + 100000u;
        return;
    }
    unsigned connected = 0;
    for (unsigned n = 1; n <= p->host.max_ports; n++)
        if (xhci_portsc(&p->host, n) & 1u) { connected = n; break; }
    if (connected) {
        int result = ncm_open(&p->ncm, &p->host, 0, 1);
        if (result) { failed(s, i, result); return; }
        ncm_proxy_init(&p->proxy, &p->ncm, 0);
        ncm_attach_batch(&p->ncm, (uint8_t *)s + i * HOST_SLOT_BYTES + HOST_BATCH_OFFSET,
                         NCM_BATCH_BYTES);
        ncm_attach_side(&p->ncm, (uint8_t *)s + i * HOST_SLOT_BYTES + HOST_SIDE_OFFSET,
                        NCM_SIDE_BYTES);
        ncm_proxy_announce(&p->proxy);
        st->connected = 1;
        p->failures = 0;
    } else if (now >= p->deadline) {
        if (stop_port(s, i, 0)) { failed(s, i, -14); return; }
        p->next_role = Q1N1_USB_DEVICE;
        p->retry_at = now + 100000u;
    }
}

void q1n1_usb_ports_tick(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) service_port(s, i);
    s->busy = 0;
}

void q1n1_usb_ports_poll(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        service_port(s, i);
        maintain_port(s, i);
    }
    s->busy = 0;
}

/* From inside a P_CALL (call-service.h): service the configured NCM links
 * only. No discovery, role change or reset -- the link carrying the pending
 * P_CALL reply must look exactly the same when the call returns. */
void q1n1_usb_ports_call_poll(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++)
        if (s->status[i].role == Q1N1_USB_HOST && host_present(&s->port[i]))
            ncm_proxy_call_poll(&s->port[i].proxy);
#ifdef Q1N1_GADGET_NCM
        else if (s->status[i].role == Q1N1_USB_DEVICE && s->port[i].ncm.gadget) {
            /* A device-role link is serviced through its controller's events:
             * block completions, and any EP0 request the Mac makes mid-run
             * (unanswered for long, it resets the device). The ACM pipe only
             * queues what arrives. No role change or stop happens here. */
            note_service(s, i);
            qdwc3_poll(&s->port[i].device);
            if (ncm_gadget_up(&s->port[i].ncm)) ncm_proxy_call_poll(&s->port[i].proxy);
        }
#endif
    s->busy = 0;
}

static void io_poll(void *ctx) { q1n1_usb_ports_poll(ctx); }
static int io_ready(void *ctx)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->selected >= 0) return port_ready(s, (unsigned)s->selected);
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) if (port_ready(s, i)) return 1;
    return 0;
}
#ifdef Q1N1_GADGET_NCM
/* A device-role port has two transports; take the one this request started
 * on, or whichever has bytes first. */
static size_t device_read(struct q1n1_usb_port *p, unsigned i, uint8_t *buffer, size_t size)
{
    size_t got = 0;
    if (via[i] != VIA_NCM && qdwc3_ready(&p->device, 0)) {
        got = qdwc3_read(&p->device, 0, buffer, size);
        if (got) via[i] = VIA_ACM;
    }
    if (!got && via[i] != VIA_ACM && gadget_ready(p)) {
        got = p->proxy.io.read(p->proxy.io.ctx, buffer, size);
        if (got) via[i] = VIA_NCM;
    }
    return got;
}
#endif
static size_t io_read(void *ctx, uint8_t *buffer, size_t size)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->busy) return 0;
    s->busy = 1;
    size_t got = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if ((s->selected >= 0 && s->selected != (int)i) || !port_ready(s, i)) continue;
        struct q1n1_usb_port *p = &s->port[i];
#ifdef Q1N1_GADGET_NCM
        got = s->status[i].role == Q1N1_USB_DEVICE
            ? device_read(p, i, buffer, size)
            : p->proxy.io.read(p->proxy.io.ctx, buffer, size);
#else
        got = s->status[i].role == Q1N1_USB_DEVICE
            ? qdwc3_read(&p->device, 0, buffer, size)
            : p->proxy.io.read(p->proxy.io.ctx, buffer, size);
#endif
        if (got) { s->selected = (int)i; last_traffic_us = xhci_now_us(); break; }
    }
    s->busy = 0;
    return got;
}
static size_t io_write(void *ctx, const uint8_t *buffer, size_t size)
{
    struct q1n1_usb_ports *s = ctx;
    if (s->busy) return 0;
    s->busy = 1;
    size_t sent = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if ((s->selected >= 0 && s->selected != (int)i) || !port_ready(s, i)) continue;
        struct q1n1_usb_port *p = &s->port[i];
#ifdef Q1N1_GADGET_NCM
        sent = s->status[i].role == Q1N1_USB_DEVICE && via[i] != VIA_NCM
            ? qdwc3_write(&p->device, 0, buffer, size)
            : p->proxy.io.write(p->proxy.io.ctx, buffer, size);
#else
        sent = s->status[i].role == Q1N1_USB_DEVICE
            ? qdwc3_write(&p->device, 0, buffer, size)
            : p->proxy.io.write(p->proxy.io.ctx, buffer, size);
#endif
        if (sent) last_traffic_us = xhci_now_us();
        break;
    }
    s->busy = 0;
    return sent;
}
#ifdef Q1N1_GADGET_NCM
static void io_end(void *ctx)
{
    ((struct q1n1_usb_ports *)ctx)->selected = -1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) via[i] = VIA_ANY;
}
static int *io_checksum_state(void *ctx)
{
    struct q1n1_usb_ports *s = ctx;
    unsigned i = s->selected >= 0 ? (unsigned)s->selected : 0;
    return via[i] == VIA_NCM ? &s->port[i].gadget_checksum_state : &s->port[i].checksum_state;
}
#else
static void io_end(void *ctx) { ((struct q1n1_usb_ports *)ctx)->selected = -1; }
static int *io_checksum_state(void *ctx)
{
    struct q1n1_usb_ports *s = ctx;
    return &s->port[s->selected >= 0 ? (unsigned)s->selected : 0].checksum_state;
}
#endif

struct q1n1_usb_ports *q1n1_usb_ports_init(uint8_t *arena, uint64_t size, int adopt)
{
    if (!arena || ((uintptr_t)arena & 0xffff) || size < Q1N1_USB_ARENA_SIZE) return NULL;
    struct q1n1_usb_ports *s = (void *)arena;
    int valid = adopt && s->magic == PORTS_MAGIC && s->version == PORTS_VERSION && s->size == sizeof(*s);
    if (!valid) memset(s, 0, sizeof(*s));
    s->busy = 1;
    s->selected = -1;
#ifdef Q1N1_GADGET_NCM
    ports_started_us = xhci_now_us();
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) deferring[i] = 0, serviced_us[i] = 0;
#endif
    s->io = (struct q1n1_io){.read = io_read, .write = io_write, .poll = io_poll,
                           .ready = io_ready, .end = io_end, .ctx = s,
                           .checksum_state = io_checksum_state};
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        struct q1n1_usb_port *p = &s->port[i];
        struct q1n1_usb_port_status *st = &s->status[i];
        uintptr_t base = i ? 0x0a800000u : 0x0a600000u;
        /* Shared pointers are valid only for this exact arena and layout. */
        int keep = valid && p->base == base && p->host.base == base &&
                   p->ncm.x == &p->host && st->role == Q1N1_USB_HOST &&
                   p->host.dma == arena + i * HOST_SLOT_BYTES + 0x10000 + QDWC3_ARENA_SIZE &&
                   ((qdwc3_rd(base + GCTL) >> 12) & 3) == 1 &&
                   xhci_running(&p->host) && host_present(p);
        if (!keep) {
            if (valid && p->base == base && st->role != Q1N1_USB_UNSUPPORTED && stop_port(s, i, 0)) {
                st->role = Q1N1_USB_UNSUPPORTED;
                continue;
            }
            memset(p, 0, sizeof(*p));
            memset(st, 0, sizeof(*st));
            p->base = base;
            p->qscratch = base + 0xf8800;
            p->device_dma = arena + i * HOST_SLOT_BYTES + 0x10000;
            p->host_dma = p->device_dma + QDWC3_ARENA_SIZE;
            if (snapshot(p)) { st->role = Q1N1_USB_UNSUPPORTED; continue; }
            p->next_role = ((p->saved.gctl >> 12) & 3) == 1 ? Q1N1_USB_HOST : Q1N1_USB_DEVICE;
        } else {
            ncm_proxy_rebind(&p->proxy, &p->ncm);
            p->proxy.head = p->proxy.tail = 0; /* Drop incomplete old requests. */
            ncm_attach_batch(&p->ncm, arena + i * HOST_SLOT_BYTES + HOST_BATCH_OFFSET,
                             NCM_BATCH_BYTES);
            ncm_attach_side(&p->ncm, arena + i * HOST_SLOT_BYTES + HOST_SIDE_OFFSET,
                            NCM_SIDE_BYTES);
        }
        st->base = base;
        st->device_stats = (uintptr_t)&p->device.stats;
        st->host_stats = (uintptr_t)&p->host.stats;
        st->ncm_stats = (uintptr_t)&p->ncm.stats;
        st->proxy_stats = (uintptr_t)&p->proxy.stats;
    }
    s->magic = PORTS_MAGIC;
    s->version = PORTS_VERSION;
    s->size = sizeof(*s);
    s->busy = 0;
    return s;
}

int q1n1_usb_ports_handoff(struct q1n1_usb_ports *s)
{
    if (!s) return 0;
    s->busy = 1;
    int result = 0;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++)
        if (s->status[i].role == Q1N1_USB_DEVICE && stop_port(s, i, 0)) result = -1;
    s->selected = -1;
    s->busy = 0;
    return result;
}
void q1n1_usb_ports_stop(struct q1n1_usb_ports *s)
{
    if (!s) return;
    s->busy = 1;
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) stop_port(s, i, 1);
    s->magic = 0;
    s->busy = 0;
}
uint64_t q1n1_usb_ports_refresh(struct q1n1_usb_ports *s)
{
    if (!s || s->busy) return 1;
    s->busy = 1;
#ifdef Q1N1_GADGET_NCM
    ports_started_us = xhci_now_us();   /* an explicit request reopens discovery */
#endif
    for (unsigned i = 0; i < Q1N1_USB_PORTS; i++) {
        if (s->selected == (int)i || s->status[i].connected) continue;
        if (s->status[i].role == Q1N1_USB_UNSUPPORTED) continue;
        if (stop_port(s, i, 0)) { s->busy = 0; return 2; }
        s->port[i].retry_at = 0;
    }
    s->busy = 0;
    return 0;
}
