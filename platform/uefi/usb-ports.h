/* SPDX-License-Identifier: MIT */
#pragma once
#include "qcom-dwc3.h"
#include "ncm-proxy.h"

#define Q1N1_USB_PORTS 2
#define Q1N1_USB_ARENA_SIZE (4u << 20)
#define Q1N1_USB_HOST_ARENA (256u << 10)

enum q1n1_usb_role { Q1N1_USB_OFF, Q1N1_USB_HOST, Q1N1_USB_DEVICE, Q1N1_USB_UNSUPPORTED };

/* Stable, all-u64 diagnostic records exposed through bootinfo. */
struct q1n1_usb_port_status {
    uint64_t base, role, connected, attempts, disconnects, last_error;
    uint64_t device_stats, host_stats, ncm_stats, proxy_stats;
};

struct q1n1_usb_port {
    struct qdwc3 device;
    struct qdwc3_saved saved;
    struct xhci host;
    struct ncm ncm;
    struct ncm_proxy proxy;
    uintptr_t base, qscratch;
    uint8_t *device_dma, *host_dma;
    uint64_t deadline, started, activity, down_since, retry_at, suspended_since;
    uint32_t qs_hs, qs_ss, failures, next_role, console_ready;
    int checksum_state;
};

struct q1n1_usb_ports {
    uint64_t magic, version, size;
    struct q1n1_usb_port_status status[Q1N1_USB_PORTS];
    struct q1n1_usb_port port[Q1N1_USB_PORTS];
    struct q1n1_io io;
    int selected;
    volatile int busy;
};

/* The arena is reserved at the top of the existing host-scratch heap. */
struct q1n1_usb_ports *q1n1_usb_ports_init(uint8_t *arena, uint64_t size, int adopt);
void q1n1_usb_ports_poll(struct q1n1_usb_ports *s);
void q1n1_usb_ports_tick(struct q1n1_usb_ports *s);
void q1n1_usb_ports_call_poll(struct q1n1_usb_ports *s);
int q1n1_usb_ports_handoff(struct q1n1_usb_ports *s);
void q1n1_usb_ports_stop(struct q1n1_usb_ports *s);
uint64_t q1n1_usb_ports_refresh(struct q1n1_usb_ports *s);

#ifdef Q1N1_GADGET_NCM
/* The proxy loop's own costs and the host's NAK-probe control, published
 * through qdwc3_ncm_table.loop for a16-link-rearm.py. Stage memory, like the
 * function counters: nothing here may enter struct q1n1_usb_ports. All u64. */
struct q1n1_loop_stats {
    uint64_t panel_redraws, panel_max_us, panels_deferred; /* main.c's status panel */
    uint64_t battery_max_us;    /* longest live-battery tick (main.c) */
    uint64_t maintain_max_us;   /* longest role/discovery step of any port */
    uint64_t hold_request_ms;   /* written by the host: arm no OUT transfer for this long, once */
    uint64_t hold_last_us;      /* how long the last hold actually lasted */
};
extern struct q1n1_loop_stats q1n1_loop_stats;
/* No request in progress and nothing moved on any link for `quiet_us`: the
 * loop may do something slow (a panel redraw) without holding up a transfer. */
int q1n1_usb_ports_quiet(const struct q1n1_usb_ports *s, uint64_t quiet_us);
#endif
