/* SPDX-License-Identifier: MIT */
/* Q1N1_GADGET_NCM: q1n1 as a CDC-NCM device, the Mac as the USB host.
 *
 * Real qcom-dwc3.c, ncm.c and ncm-proxy.c, built with the define, over a model
 * of the DWC3 registers and event ring (as tools/test-a16-qcom-dwc3.c). The
 * test plays the host: it enumerates the composite device, makes the NCM class
 * requests a host driver makes, selects the data interface, then exchanges
 * NTB16 blocks carrying IPv6 neighbour discovery and the proxy's UDP stream.
 * Nothing here models macOS's own NCM driver; what it checks is that the
 * function follows CDC NCM 1.0 the way the host side of this tree (ncm.c)
 * already reads it from the Mac. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../platform/uefi/qcom-dwc3.h"
#include "../platform/uefi/ncm.h"
#include "../platform/uefi/ncm-proxy.h"

#ifndef Q1N1_GADGET_NCM
#error build with -DQ1N1_GADGET_NCM
#endif

#define BASE 0x0a800000u                /* USB1, so the controller byte is 1 */
#define QS 0x0a8f8800u
static uint32_t regs[0x10000 / 4], qs[0x100 / 4];
static uint64_t trb_addr[10];
static unsigned started[10], completed[10], ended[10], stalls_set;
static uint32_t write_pointer;
static unsigned checks, delays, host_reading;
static uint64_t now_us = 1;
#define CHECK(x) do { assert(x); checks++; } while (0)

uint64_t xhci_now_us(void) { return now_us; }
static uint32_t *reg(uint32_t off) { return &regs[off / 4]; }
uint32_t qdwc3_rd(uintptr_t a)
{
    if (a >= QS && a < QS + 0x100) return qs[(a - QS) / 4];
    assert(a >= BASE && a < BASE + 0x10000 && !(a & 3));
    return regs[(a - BASE) / 4];
}
void qdwc3_wr(uintptr_t a, uint32_t v)
{
    if (a >= QS && a < QS + 0x100) { qs[(a - QS) / 4] = v; return; }
    assert(a >= BASE && a < BASE + 0x10000 && !(a & 3));
    uint32_t off = (uint32_t)(a - BASE);
    if (off == 0xc40c) { assert(v <= *reg(0xc40c)); *reg(0xc40c) -= v; return; }
    *reg(off) = v;
    if (off == 0xc704) {
        if (v & (1u << 30)) *reg(0xc704) &= ~(1u << 30);
        *reg(0xc70c) = (*reg(0xc70c) & ~(1u << 22)) | ((v >> 31) ? 0 : (1u << 22));
    }
    if (off >= 0xc80c && off < 0xc80c + 16 * 10 && !((off - 0xc80c) % 16)) {
        unsigned ep = (off - 0xc80c) / 16, cmd = v & 0xff;
        if (cmd == 0x04) stalls_set++;
        if (cmd == 0x06) {
            trb_addr[ep] = (uint64_t)*reg(0xc808 + ep * 16) << 32 | *reg(0xc804 + ep * 16);
            started[ep]++;
        }
        if (cmd == 0x08) ended[ep]++;
        *reg(off) = (v & ~(1u << 10)) | (cmd == 0x06 ? (ep + 1) << 16 : 0);
    }
}

static uint32_t le16(const uint8_t *p) { return p[0] | p[1] << 8; }
static uint32_t le32(const uint8_t *p) { return le16(p) | le16(p + 2) << 16; }
static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static uint8_t *trb(unsigned ep) { assert(trb_addr[ep]); return (uint8_t *)(uintptr_t)trb_addr[ep]; }
static uint8_t *buffer(unsigned ep) { const uint8_t *t = trb(ep); return (uint8_t *)(uintptr_t)(le32(t) | (uint64_t)le32(t + 4) << 32); }
static uint32_t trb_len(unsigned ep) { return le32(trb(ep) + 8) & 0xffffff; }
static uint32_t trb_type(unsigned ep) { return le32(trb(ep) + 12) >> 4 & 0x3f; }
static int trb_owned(unsigned ep) { return trb_addr[ep] && (le32(trb(ep) + 12) & 1); }

static struct qdwc3 d;
static void inject(uint32_t event)
{
    uint8_t *events = (uint8_t *)(uintptr_t)(*reg(0xc400) | (uint64_t)*reg(0xc404) << 32);
    memcpy(events + write_pointer, &event, 4);
    write_pointer = (write_pointer + 4) % 4096;
    *reg(0xc40c) += 4;
}
static uint32_t ep_event(unsigned ep, unsigned kind) { return ep << 1 | kind << 6; }
static uint32_t dev_event(unsigned type) { return 1 | type << 8; }
/* Finish the active TRB on `ep` with `bytes` moved; the event is queued only. */
static void finish(unsigned ep, uint32_t bytes)
{
    uint8_t *t = trb(ep);
    uint32_t len = trb_len(ep), remaining, ctrl = le32(t + 12) & ~1u;
    assert(bytes <= len && (le32(t + 12) & 1));
    remaining = len - bytes;
    memcpy(t + 8, &remaining, 4);
    memcpy(t + 12, &ctrl, 4);
    completed[ep]++;
    inject(ep_event(ep, 1));
}
static void complete(unsigned ep, uint32_t bytes) { finish(ep, bytes); qdwc3_poll(&d); }
static void not_ready(unsigned ep) { inject(ep_event(ep, 3)); qdwc3_poll(&d); }

/* The host reads IN blocks as soon as they are offered while `host_reading`;
 * each one is kept for the checks below. ncm.c waits by delaying and polling. */
#define BLOCKS 64
static uint8_t in_blocks[BLOCKS][QDWC3_NCM_NTB_MAX];
static uint32_t in_length[BLOCKS], in_count;
static void host_takes_in(void)
{
    if (!trb_owned(9)) return;
    assert(in_count < BLOCKS);
    in_length[in_count] = trb_len(9);
    memcpy(in_blocks[in_count++], buffer(9), trb_len(9));
    finish(9, trb_len(9));
}
void qdwc3_delay_us(uint64_t us)
{
    delays++;
    now_us += us;
    if (host_reading) host_takes_in();
}
/* The last block a send left on the wire, taken by the host now. */
static void drain(void)
{
    if (!trb_owned(9)) return;
    host_takes_in();
    qdwc3_poll(&d);
}
static uint32_t be16(const uint8_t *p) { return (uint32_t)p[0] << 8 | p[1]; }

/* ncm.c's host role is not exercised here; reaching it is a failure. */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wunused-parameter"
#define NOT_HERE { fprintf(stderr, "host-role xHCI call in a device-role test\n"); abort(); }
uint8_t *xhci_alloc(struct xhci *x, uint64_t s, uint64_t a) NOT_HERE
uint32_t xhci_wait_port(struct xhci *x, uint32_t ms) NOT_HERE
int xhci_address_device(struct xhci *x, struct xhci_device *v, uint32_t p) NOT_HERE
int xhci_configure_endpoints(struct xhci *x, struct xhci_device *v, const uint32_t *dci,
                             const uint32_t *type, const uint32_t *packet, uint32_t n) NOT_HERE
void xhci_release(struct xhci *x, struct xhci_device *v) NOT_HERE
int xhci_control(struct xhci *x, struct xhci_device *v, uint32_t t, uint32_t r, uint32_t va, uint32_t i,
                 uint8_t *b, uint32_t l, uint32_t ms) NOT_HERE
int xhci_post(struct xhci *x, struct xhci_device *v, uint32_t dci, uint8_t *b, uint32_t l) NOT_HERE
int xhci_reap(struct xhci *x, struct xhci_device *v, uint32_t dci, uint32_t l) NOT_HERE
int xhci_send(struct xhci *x, struct xhci_device *v, uint32_t dci, uint8_t *b, uint32_t l, uint32_t ms) NOT_HERE
uint8_t *xhci_send_start(struct xhci *x, struct xhci_device *v, uint32_t dci, uint8_t *b, uint32_t l) NOT_HERE
int xhci_sent(struct xhci *x, const uint8_t *slot) NOT_HERE
#pragma clang diagnostic pop

static uint8_t last_in[512];
static uint32_t last_in_len;
/* One control transfer as the host drives it. Returns the IN data length, or
 * -1 when the device stalled. */
static int control(const uint8_t setup[8], const uint8_t *out_data, uint32_t out_len)
{
    assert(trb_type(0) == 2 && trb_len(0) == 8);
    memcpy(buffer(0), setup, 8);
    unsigned stalls_before = stalls_set;
    complete(0, 8);
    if (stalls_set != stalls_before) { assert(trb_type(0) == 2); return -1; }
    uint16_t length = setup[6] | setup[7] << 8;
    if (length && (setup[0] & 0x80)) {
        not_ready(1);
        assert(trb_type(1) == 5);
        last_in_len = trb_len(1);
        memcpy(last_in, buffer(1), last_in_len);
        complete(1, last_in_len);
        not_ready(0);
        assert(trb_type(0) == 4);
        complete(0, 0);
    } else {
        if (length) {
            not_ready(0);
            assert(trb_type(0) == 5 && trb_len(0) == 64);
            memcpy(buffer(0), out_data, out_len);
            complete(0, out_len);
        }
        not_ready(1);
        assert(trb_type(1) == (length ? 4u : 3u));
        complete(1, 0);
        last_in_len = 0;
    }
    assert(trb_type(0) == 2);
    return (int)last_in_len;
}
#define SETUP(t, r, v, i, l) ((const uint8_t[8]){(t), (r), (uint8_t)(v), (uint8_t)((v) >> 8), (i), 0, \
                                                 (uint8_t)(l), (uint8_t)((l) >> 8)})

/* ---- frames, as the Mac would send them ---- */
static const uint8_t mac_host[6] = {0x02, 0x51, 0x31, 0x4e, 'H', 1};   /* iMACAddress */
static const uint8_t mac_own[6] = {0x02, 0x51, 0x31, 0x4e, 'D', 1};
static const uint8_t peer_ip[16] = {0xfe, 0x80, [15] = 1};
static const uint8_t fixed_ip[16] = {0xfe, 0x80, [14] = 0x49, [15] = 0x19};

static uint32_t sum16(const uint8_t *p, uint32_t n, uint32_t s)
{
    for (uint32_t k = 0; k + 1 < n; k += 2) s += (uint32_t)p[k] << 8 | p[k + 1];
    if (n & 1) s += (uint32_t)p[n - 1] << 8;
    return s;
}
static int transport_ok(const uint8_t *ip, uint32_t next)
{
    uint32_t length = (uint32_t)ip[4] << 8 | ip[5];
    uint32_t s = sum16(ip + 8, 32, 0) + length + next;
    s = sum16(ip + 40, length, s);
    while (s >> 16) s = (s & 0xffff) + (s >> 16);
    return s == 0xffff;
}
static uint32_t ipv6(uint8_t *f, const uint8_t *dst_mac, const uint8_t *dst, uint32_t next, uint32_t length)
{
    memcpy(f, dst_mac, 6); memcpy(f + 6, mac_host, 6); f[12] = 0x86; f[13] = 0xdd;
    uint8_t *ip = f + 14;
    memset(ip, 0, 40);
    ip[0] = 0x60; ip[4] = (uint8_t)(length >> 8); ip[5] = (uint8_t)length; ip[6] = (uint8_t)next; ip[7] = 255;
    memcpy(ip + 8, peer_ip, 16); memcpy(ip + 24, dst, 16);
    return 54;
}
static uint32_t solicitation(uint8_t *f)
{
    static const uint8_t group_mac[6] = {0x33, 0x33, 0xff, 0x00, 0x49, 0x19};
    static const uint8_t group[16] = {0xff, 0x02, [11] = 1, [12] = 0xff, [13] = 0, [14] = 0x49, [15] = 0x19};
    uint32_t at = ipv6(f, group_mac, group, 58, 32);
    memset(f + at, 0, 32);
    f[at] = 135; memcpy(f + at + 8, fixed_ip, 16); f[at + 24] = 1; f[at + 25] = 1; memcpy(f + at + 26, mac_host, 6);
    return at + 32;
}
static uint32_t datagram(uint8_t *f, uint32_t port, const void *payload, uint32_t n)
{
    uint32_t at = ipv6(f, mac_own, fixed_ip, 17, 8 + n);
    f[at] = 50000 >> 8; f[at + 1] = 50000 & 0xff; f[at + 2] = (uint8_t)(port >> 8); f[at + 3] = (uint8_t)port;
    f[at + 4] = (uint8_t)((8 + n) >> 8); f[at + 5] = (uint8_t)(8 + n); f[at + 6] = f[at + 7] = 0;
    memcpy(f + at + 8, payload, n);
    return at + 8 + n;
}
/* NTB16 from the host: datagrams 4-aligned after the NTH, then the NDP. */
static uint32_t host_ntb(uint8_t *out, uint8_t frames[][1600], const uint32_t *lengths, unsigned count)
{
    static uint16_t sequence;
    uint32_t at = 12, index[8];
    for (unsigned k = 0; k < count; k++) {
        index[k] = at;
        memcpy(out + at, frames[k], lengths[k]);
        at = (at + lengths[k] + 3) & ~3u;
    }
    uint32_t ndp = at, total = ndp + 8 + 4 * (count + 1);
    memcpy(out, "NCMH", 4); put16(out + 4, 12); put16(out + 6, sequence++); put16(out + 8, total); put16(out + 10, ndp);
    memcpy(out + ndp, "NCM0", 4); put16(out + ndp + 4, 8 + 4 * (count + 1)); put16(out + ndp + 6, 0);
    for (unsigned k = 0; k < count; k++) { put16(out + ndp + 8 + 4 * k, index[k]); put16(out + ndp + 10 + 4 * k, lengths[k]); }
    put16(out + ndp + 8 + 4 * count, 0); put16(out + ndp + 10 + 4 * count, 0);
    return total;
}
/* Hand an NTB to the function's OUT endpoint, as the host's bulk OUT. */
static void host_sends(const uint8_t *ntb, uint32_t total)
{
    assert(trb_owned(8) && trb_type(8) == 1 && trb_len(8) == QDWC3_NCM_NTB_MAX);
    memcpy(buffer(8), ntb, total);
    complete(8, total);
}
/* Datagrams of a captured IN block, checked against NCM 1.0 3.2/3.3. */
static unsigned walk_in(unsigned block, const uint8_t *out[], uint32_t lengths[], unsigned max)
{
    const uint8_t *b = in_blocks[block];
    uint32_t total = in_length[block];
    assert(!memcmp(b, "NCMH", 4) && le16(b + 4) == 12 && le16(b + 8) == total);
    uint32_t ndp = le16(b + 10);
    assert(ndp % 4 == 0 && ndp + 16 <= total && !memcmp(b + ndp, "NCM0", 4));
    unsigned n = 0;
    for (uint32_t e = ndp + 8; e + 4 <= ndp + le16(b + ndp + 4); e += 4) {
        uint32_t index = le16(b + e), length = le16(b + e + 2);
        if (!index && !length) break;
        assert(index % 4 == 0 && index + length <= ndp && n < max);
        out[n] = b + index; lengths[n++] = length;
    }
    return n;
}

static uint8_t side_seen[64];
static uint32_t side_handler(void *ctx, const uint8_t *data, uint32_t length, uint8_t *reply)
{
    (void)ctx;
    memcpy(side_seen, data, length < sizeof(side_seen) ? length : sizeof(side_seen));
    memcpy(reply, "ack:", 4);
    memcpy(reply + 4, data, length);
    return 4 + length;
}

int main(void)
{
    static const struct qdwc3_saved saved = {
        .gsbuscfg0 = 0xe, .gctl = 0x30c12004, .gusb2phycfg = 0x00102500, .dcfg = 0x00080804,
        .gsnpsid = 0x33310000 | 0x200a};
    uint8_t *arena = aligned_alloc(4096, QDWC3_ARENA_SIZE);
    assert(arena);
    *reg(0xc120) = saved.gsnpsid;
    CHECK(qdwc3_takeover(&d, BASE, QS, &saved, arena, QDWC3_ARENA_SIZE) == 0);
    /* Its own buffers: two transmit blocks and the notification, inside the arena and apart. */
    CHECK(d.ncm.tx[0] && d.ncm.tx[1] && d.ncm.notify && d.ncm.tx[0] != d.ncm.tx[1] &&
          d.ncm.tx[1] + QDWC3_NCM_NTB_MAX <= d.ncm.notify && d.ncm.notify + 64 <= arena + QDWC3_ARENA_SIZE);
    CHECK(d.ep[7].buffer == d.ncm.notify && d.ep[7].buffer != d.ep[8].buffer);

    static struct ncm link;
    static struct ncm_proxy proxy;
    ncm_open_gadget(&link, &d);
    ncm_proxy_init(&proxy, &link, 0);
    ncm_proxy_side_port(4920, side_handler, NULL);
    CHECK(!memcmp(link.mac, mac_own, 6) && link.x == NULL && link.gadget == &d && !ncm_gadget_up(&link));
    uint8_t frame[1600];
    CHECK(ncm_send(&link, frame, 60) < 0);           /* nothing is carried before the host opens it */

    /* Enumerate like a host: reset, connect, descriptors, address, configuration. */
    inject(dev_event(1)); qdwc3_poll(&d);
    inject(dev_event(2)); qdwc3_poll(&d);
    CHECK(control(SETUP(0x80, 6, 0x0100, 0, 18), NULL, 0) == 18 && last_in[4] == 2);
    CHECK(control(SETUP(0, 5, 7, 0, 0), NULL, 0) == 0);
    CHECK(control(SETUP(0x80, 6, 0x0200, 0, 255), NULL, 0) == 130);
    uint8_t config[130];
    memcpy(config, last_in, sizeof(config));
    /* Function 0 is the default build's ACM port, byte for byte. */
    static const uint8_t acm[] = {
        9, 4, 0, 0, 1, 2, 2, 0, 0,  5, 0x24, 6, 0, 1,  7, 5, 0x81, 3, 64, 0, 10,
        9, 4, 1, 0, 2, 0x0a, 0, 0, 0,  7, 5, 0x02, 2, 0x00, 0x02, 10,  7, 5, 0x82, 2, 0x00, 0x02, 10};
    CHECK(le16(config + 2) == 130 && config[4] == 4 && !memcmp(config + 9, acm, sizeof(acm)));
    /* Walk the rest as a host NCM driver does (ncm.c find_function's rules). */
    unsigned control_if = 99, data_if = 99, mac_index = 0, alt1_in = 0, alt1_out = 0, alt0_eps = 0;
    unsigned cur_if = 99, cur_alt = 99, ncm_version = 0, int_ep = 0, union_seen = 0;
    for (unsigned at = 9 + sizeof(acm); at < 130; at += config[at]) {
        const uint8_t *item = config + at;
        assert(item[0] >= 2 && at + item[0] <= 130);
        if (item[1] == 4) {
            cur_if = item[2]; cur_alt = item[3];
            if (item[5] == 2 && item[6] == 0x0d) control_if = item[2];
            if (cur_if == data_if) CHECK(item[5] == 0x0a && item[7] == 1 && item[4] == (cur_alt ? 2 : 0));
        } else if (item[1] == 0x24 && item[2] == 0x06) { union_seen = 1; CHECK(item[3] == control_if); data_if = item[4]; }
        else if (item[1] == 0x24 && item[2] == 0x0f) { mac_index = item[3]; CHECK(le16(item + 8) == 1514); }
        else if (item[1] == 0x24 && item[2] == 0x1a) ncm_version = le16(item + 3);
        else if (item[1] == 5 && cur_if == control_if) int_ep = item[2];
        else if (item[1] == 5 && cur_if == data_if) {
            if (cur_alt == 0) alt0_eps++;
            else if (item[2] & 0x80) alt1_in = item[2] | (le16(item + 4) << 8);
            else alt1_out = item[2] | (le16(item + 4) << 8);
            CHECK((item[3] & 3) == 2);
        }
    }
    CHECK(union_seen && control_if == 2 && data_if == 3 && ncm_version == 0x0100 && int_ep == 0x83);
    CHECK(!alt0_eps && alt1_in == (0x84 | 512 << 8) && alt1_out == (0x04 | 512 << 8));
    CHECK(control(SETUP(0x80, 6, 0x0300 | mac_index, 0x09, 255), NULL, 0) == 26);
    char text[13] = {0};
    for (unsigned k = 0; k < 12; k++) text[k] = (char)last_in[2 + 2 * k];
    CHECK(!strcmp(text, "0251314E4801"));            /* the host's address, USB1 */
    CHECK(control(SETUP(0, 9, 1, 0, 0), NULL, 0) == 0 && d.stats.configured && *reg(0xc720) == 0x3bb);

    /* NCM class requests, as a host driver binds. */
    CHECK(control(SETUP(0xa1, 0x80, 0, 2, 28), NULL, 0) == 28);
    CHECK(le16(last_in) == 28 && le16(last_in + 2) == 1 && le32(last_in + 4) == QDWC3_NCM_NTB_MAX &&
          le16(last_in + 8) == 4 && le16(last_in + 12) == 4 && le32(last_in + 16) == QDWC3_NCM_NTB_MAX &&
          le16(last_in + 20) == 4 && le16(last_in + 24) == 4 && le16(last_in + 26) == 0);
    CHECK(control(SETUP(0xa1, 0x83, 0, 2, 2), NULL, 0) == 2 && le16(last_in) == 0);
    CHECK(control(SETUP(0x21, 0x84, 0, 2, 0), NULL, 0) == 0);
    CHECK(control(SETUP(0x21, 0x84, 1, 2, 0), NULL, 0) == -1);          /* NTB32 is not offered */
    const uint8_t input_size[4] = {0x00, 0x20, 0, 0};                    /* 8192 */
    CHECK(control(SETUP(0x21, 0x86, 0, 2, 4), input_size, 4) == 0 && qdwc3_ncm_tx_limit(&d) == 8192);
    CHECK(control(SETUP(0xa1, 0x85, 0, 2, 4), NULL, 0) == 4 && le32(last_in) == 8192 && d.stats.ncm.tx_limit == 8192);
    CHECK(control(SETUP(0x21, 0x43, 0x0c, 2, 0), NULL, 0) == 0 && d.ncm.filter == 0x0c);
    CHECK(control(SETUP(0x21, 0x22, 3, 2, 0), NULL, 0) == -1);          /* not an ACM port any more */
    CHECK(control(SETUP(0xa1, 0x87, 0, 2, 4), NULL, 0) == -1);          /* max datagram size: not advertised */
    CHECK(control(SETUP(0x21, 0x22, 3, 0, 0), NULL, 0) == 0 && qdwc3_ready(&d, 0) && !qdwc3_ready(&d, 1));
    CHECK(control(SETUP(0x81, 10, 0, 3, 1), NULL, 0) == 1 && last_in[0] == 0);   /* GET_INTERFACE */
    qdwc3_poll(&d);
    CHECK(started[8] == 0 && started[7] == 0 && !ncm_gadget_up(&link));  /* alt 0: idle */

    /* Selecting alternate setting 1 opens the link and starts the notifications. */
    CHECK(control(SETUP(0x01, 11, 1, 3, 0), NULL, 0) == 0 && ncm_gadget_up(&link) && d.stats.ncm.alt == 1);
    CHECK(control(SETUP(0x81, 10, 0, 3, 1), NULL, 0) == 1 && last_in[0] == 1);
    CHECK(started[7] == 1 && trb_len(7) == 16);
    const uint8_t *n7 = buffer(7);
    CHECK(n7[0] == 0xa1 && n7[1] == 0x2a && le16(n7 + 4) == 2 && le16(n7 + 6) == 8 &&
          le32(n7 + 8) == 480000000u && le32(n7 + 12) == 480000000u);
    complete(7, 16);
    CHECK(started[7] == 2 && trb_len(7) == 8 && n7[1] == 0x00 && le16(n7 + 2) == 1 && le16(n7 + 4) == 2);
    complete(7, 8);
    CHECK(started[7] == 2 && d.stats.ncm.notifications == 2);
    CHECK(trb_owned(8) && trb_len(8) == QDWC3_NCM_NTB_MAX && started[8] == 1);

    /* The Mac solicits fe80::4919 and sends a proxy datagram in one block. */
    host_reading = 1;
    uint8_t frames[4][1600], ntb[QDWC3_NCM_NTB_MAX];
    uint32_t lengths[4];
    lengths[0] = solicitation(frames[0]);
    lengths[1] = datagram(frames[1], 4919, "hello", 5);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 2));
    CHECK(d.ncm.rx_held && started[8] == 1);         /* held: the host is NAKed, not dropped */
    proxy.io.poll(&proxy);
    CHECK(proxy.stats.solicitations == 1 && proxy.have_peer && !memcmp(proxy.peer_mac, mac_host, 6));
    uint8_t got[8];
    CHECK(proxy.io.read(&proxy, got, sizeof(got)) == 5 && !memcmp(got, "hello", 5));
    CHECK(in_count == 0 && trb_owned(9));             /* the advertisement is on the wire */
    drain();
    CHECK(in_count == 1);
    const uint8_t *datagrams[16];
    uint32_t sizes[16];
    CHECK(walk_in(0, datagrams, sizes, 16) == 1 && in_length[0] % 512);
    const uint8_t *na = datagrams[0];
    CHECK(!memcmp(na, mac_host, 6) && !memcmp(na + 6, mac_own, 6) && na[14 + 40] == 136 &&
          !memcmp(na + 14 + 48, fixed_ip, 16) && !memcmp(na + 14 + 66, mac_own, 6) && transport_ok(na + 14, 58));
    /* Once the block is consumed, the endpoint is re-armed for the next. */
    proxy.io.poll(&proxy);
    CHECK(!d.ncm.rx_held && started[8] == 2 && trb_owned(8));

    /* Proxy replies: several datagrams per block, every block within the host's
     * 8192-byte input size, none a whole number of packets, payload intact. */
    static uint8_t reply[20000], back[20000];
    for (unsigned k = 0; k < sizeof(reply); k++) reply[k] = (uint8_t)(k * 13 + 7);
    unsigned first = in_count;
    CHECK(proxy.io.write(&proxy, reply, sizeof(reply)) == sizeof(reply));
    drain();
    CHECK(in_count - first >= 3);
    uint32_t back_length = 0;
    for (unsigned b = first; b < in_count; b++) {
        CHECK(in_length[b] <= 8192 && in_length[b] % 512);
        unsigned count = walk_in(b, datagrams, sizes, 16);
        CHECK(count > 1 || b == in_count - 1);
        for (unsigned k = 0; k < count; k++) {
            const uint8_t *ip = datagrams[k] + 14;
            CHECK(!memcmp(datagrams[k], mac_host, 6) && ip[6] == 17 && transport_ok(ip, 17) &&
                  be16(ip + 40) == 4919 && be16(ip + 42) == 50000 && !memcmp(ip + 8, fixed_ip, 16));
            uint32_t n = sizes[k] - 62;
            memcpy(back + back_length, ip + 48, n);
            back_length += n;
        }
    }
    CHECK(back_length == sizeof(reply) && !memcmp(back, reply, sizeof(reply)));

    /* A block that would be exactly one packet gets padding instead of needing a ZLP. */
    first = in_count;
    CHECK(ncm_queue(&link, reply, 484) == 484 && ncm_flush(&link) == 1);
    drain();
    CHECK(in_count == first + 1 && in_length[first] == 516 && le16(in_blocks[first] + 10) == 500);

    /* Inside a P_CALL: the side port answers without waiting, and a reply
     * that would have to wait for the wire is refused and counted. */
    host_reading = 0;
    unsigned delays_before = delays, starts_before = started[9];
    struct ncm_side_stats side_before, side_after;
    ncm_proxy_side_stats(&side_before);
    lengths[0] = datagram(frames[0], 4920, "Q1SRstop", 8);
    lengths[1] = datagram(frames[1], 4920, "Q1SRmore", 8);
    lengths[2] = datagram(frames[2], 4919, "late", 4);
    CHECK(trb_owned(8));
    host_sends(ntb, host_ntb(ntb, frames, lengths, 3));
    ncm_proxy_call_poll(&proxy);
    ncm_proxy_side_stats(&side_after);
    CHECK(delays == delays_before && started[9] == starts_before + 1 && trb_owned(9));
    CHECK(side_after.replies == side_before.replies + 1 && side_after.reply_failures == side_before.reply_failures + 1);
    CHECK(side_after.call_dropped == side_before.call_dropped + 1 && !memcmp(side_seen, "Q1SRmore", 8));
    host_takes_in();
    qdwc3_poll(&d);
    CHECK(walk_in(in_count - 1, datagrams, sizes, 16) == 1 && !memcmp(datagrams[0] + 62, "ack:Q1SRstop", 12));

    /* Alternate setting 0 closes the link: transfers end, nothing is sent. */
    proxy.io.poll(&proxy);
    CHECK(trb_owned(8));
    unsigned ended_out = ended[8];
    CHECK(control(SETUP(0x01, 11, 0, 3, 0), NULL, 0) == 0 && !ncm_gadget_up(&link));
    CHECK(ended[8] == ended_out + 1 && !d.ep[8].in_progress && !d.ncm.rx_held);
    CHECK(ncm_send(&link, reply, 100) < 0 && proxy.io.read(&proxy, got, 1) == 0);
    CHECK(qdwc3_ncm_tx_limit(&d) == 8192);            /* the host's input size survives alt 0 */
    /* Reopened, it starts clean and works again. */
    CHECK(control(SETUP(0x01, 11, 1, 3, 0), NULL, 0) == 0 && ncm_gadget_up(&link));
    complete(7, 16); complete(7, 8);
    host_reading = 1;
    lengths[0] = datagram(frames[0], 4919, "again", 5);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 1));
    proxy.io.poll(&proxy);
    CHECK(proxy.io.read(&proxy, got, sizeof(got)) == 5 && !memcmp(got, "again", 5));
    first = in_count;
    CHECK(proxy.io.write(&proxy, (const uint8_t *)"ok", 2) == 2);
    drain();
    CHECK(in_count == first + 1 && walk_in(first, datagrams, sizes, 16) == 1 && !memcmp(datagrams[0] + 62, "ok", 2));

    /* The host re-selects the data interface while a block is half walked:
     * the rest of that block is gone (the buffer is being refilled), and the
     * walk must not continue into the next block's bytes. */
    lengths[0] = datagram(frames[0], 4919, "first", 5);
    lengths[1] = datagram(frames[1], 4919, "second", 6);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 2));
    CHECK(ncm_receive(&link, frame, sizeof(frame)) == (int)lengths[0] && !memcmp(frame + 62, "first", 5));
    CHECK(control(SETUP(0x01, 11, 0, 3, 0), NULL, 0) == 0);
    CHECK(control(SETUP(0x01, 11, 1, 3, 0), NULL, 0) == 0 && trb_owned(8));
    complete(7, 16); complete(7, 8);
    lengths[0] = datagram(frames[0], 4919, "third", 5);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 1));
    CHECK(ncm_receive(&link, frame, sizeof(frame)) == (int)lengths[0] && !memcmp(frame + 62, "third", 5));
    CHECK(ncm_receive(&link, frame, sizeof(frame)) == 0 && trb_owned(8));

    /* Back-to-back NTBs in one transfer: a block of whole packets that the
     * host does not end with a ZLP runs into the next one. Both are walked;
     * nothing in the second is lost (it was, silently, before). */
    uint64_t chained = d.stats.ncm.rx_chained, trailing = d.stats.ncm.rx_trailing;
    static uint8_t joined[QDWC3_NCM_NTB_MAX];
    lengths[0] = datagram(frames[0], 4919, reply, 934);   /* 62 + 934 = 996: the NTB is exactly 1024 */
    uint32_t first_ntb = host_ntb(joined, frames, lengths, 1);
    CHECK(first_ntb == 1024);
    lengths[0] = datagram(frames[0], 4919, "joined", 6);
    lengths[1] = datagram(frames[1], 4919, "too", 3);
    uint32_t second_ntb = host_ntb(joined + first_ntb, frames, lengths, 2);
    CHECK(trb_owned(8));
    host_sends(joined, first_ntb + second_ntb);
    uint32_t moved = 0;
    size_t piece;
    for (unsigned k = 0; k < 4; k++) proxy.io.poll(&proxy);
    while ((piece = proxy.io.read(&proxy, got, sizeof(got))) > 0) moved += (uint32_t)piece;
    CHECK(moved == 934 + 6 + 3 && d.stats.ncm.rx_chained == chained + 1 && d.stats.ncm.rx_trailing == trailing);
    CHECK(!d.ncm.rx_held && trb_owned(8));
    /* Bytes after the last NTB that are not one are counted, then dropped. */
    lengths[0] = datagram(frames[0], 4919, "x", 1);
    uint32_t one = host_ntb(joined, frames, lengths, 1);
    memset(joined + one, 0xee, 20);
    host_sends(joined, one + 20);
    for (unsigned k = 0; k < 3; k++) proxy.io.poll(&proxy);
    CHECK(proxy.io.read(&proxy, got, sizeof(got)) == 1 && d.stats.ncm.rx_trailing == trailing + 20);

    /* The Mac finds no OUT transfer armed while a block is still held. */
    uint64_t unarmed = d.stats.ncm.out_not_ready;
    lengths[0] = datagram(frames[0], 4919, "held", 4);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 1));
    CHECK(d.ncm.rx_held && !trb_owned(8));
    not_ready(8);
    CHECK(d.stats.ncm.out_not_ready == unarmed + 1 && !trb_owned(8));
    proxy.io.poll(&proxy); proxy.io.poll(&proxy);
    CHECK(proxy.io.read(&proxy, got, sizeof(got)) == 4 && trb_owned(8));

    /* An IN block that has to wait for the previous one is counted and timed. */
    drain();
    uint64_t waits = d.stats.ncm.tx_waits;
    CHECK(proxy.io.write(&proxy, reply, 12000) == 12000);  /* two 8 KiB blocks: the second waits */
    drain();
    CHECK(d.stats.ncm.tx_waits > waits && d.stats.ncm.tx_wait_max_us >= 10);

    /* Faulty host input is counted and dropped; the link keeps going. */
    uint64_t bad_signature = link.stats.bad_signature, bad_ndp = link.stats.bad_ndp;
    uint32_t total = host_ntb(ntb, frames, lengths, 1);
    memcpy(ntb, "XCMH", 4);
    host_sends(ntb, total);
    proxy.io.poll(&proxy);
    CHECK(link.stats.bad_signature == bad_signature + 1);
    proxy.io.poll(&proxy);                            /* the next poll hands the block back */
    CHECK(trb_owned(8) && !d.ncm.rx_held);
    total = host_ntb(ntb, frames, lengths, 1);
    put16(ntb + 10, total);                           /* NDP index past the block */
    host_sends(ntb, total);
    proxy.io.poll(&proxy);
    proxy.io.poll(&proxy);
    CHECK(link.stats.bad_ndp == bad_ndp + 1 && trb_owned(8) && !d.ncm.rx_held);
    uint64_t empty = d.stats.ncm.empty_blocks;
    complete(8, 0);                                   /* a lone ZLP is not a block */
    CHECK(d.stats.ncm.empty_blocks == empty + 1 && !d.ncm.rx_held && trb_owned(8));
    lengths[0] = datagram(frames[0], 4919, "still", 5);
    host_sends(ntb, host_ntb(ntb, frames, lengths, 1));
    proxy.io.poll(&proxy);
    CHECK(proxy.io.read(&proxy, got, sizeof(got)) == 5 && !memcmp(got, "still", 5));
    /* An input size below NCM's 2048-byte minimum cannot shrink blocks below one frame. */
    const uint8_t tiny[4] = {100, 0, 0, 0};
    CHECK(control(SETUP(0x21, 0x86, 0, 2, 4), tiny, 4) == 0 && qdwc3_ncm_tx_limit(&d) == QDWC3_NCM_NTB_MIN);
    first = in_count;
    CHECK(proxy.io.write(&proxy, reply, 3000) == 3000);
    drain();
    for (unsigned b = first; b < in_count; b++) CHECK(in_length[b] <= QDWC3_NCM_NTB_MIN && in_length[b] % 512);
    CHECK(in_count - first == 2);                     /* 1462 | 1462 + 262: two never share 2 KiB */

    /* A bus reset forgets the function's state, including the input size. */
    inject(dev_event(1)); qdwc3_poll(&d);
    CHECK(!d.stats.configured && !ncm_gadget_up(&link) && qdwc3_ncm_tx_limit(&d) == QDWC3_NCM_NTB_MAX);
    CHECK(d.stats.ncm.alt == 0 && d.ncm.notify_state == 0 && !d.ncm.rx_held);

    /* Stop ends every transfer and drops the VBUS override, as for ACM. */
    qdwc3_stop(&d);
    CHECK(!(*reg(0xc704) >> 31) && !(qs[0x10 / 4] & ((1u << 20) | (1u << 28))));

    free(arena);
    printf("PASS: %u device-role CDC-NCM checks (register model, NTB16, ND and UDP proxy)\n", checks);
    return 0;
}
