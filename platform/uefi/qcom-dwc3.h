/* SPDX-License-Identifier: MIT AND BSD-3-Clause */
/*
 * Portions ported from m1n1 src/usb_dwc3.c and src/usb_dwc3_regs.h (MIT):
 *   Copyright The Asahi Linux Contributors
 * DWC3 register definitions from the Linux dwc3 driver (drivers/usb/dwc3/core.h, via
 * m1n1 src/usb_dwc3_regs.h), used under BSD-3-Clause (3rdparty_licenses/LICENSE.BSD-3.dwc3):
 *   Copyright (C) 2010-2011 Texas Instruments Incorporated - http://www.ti.com
 */
/* Polled DWC3 CDC ACM device for a Qualcomm controller that firmware already
 * powered, clocked, and configured. Ported from m1n1 src/usb_dwc3.c (Asahi
 * Linux contributors): identity DMA replaces DART IOVAs, the Qualcomm
 * qscratch VBUS override replaces Apple PHY/HPM setup, and only the device
 * soft reset is used so firmware PHY configuration is preserved. */
#pragma once
#include <stdint.h>
#include <stddef.h>

#define QDWC3_PIPES 2
#define QDWC3_ARENA_SIZE (1u << 20)

/* Firmware-running register values used to restore global configuration. */
struct qdwc3_saved {
    uint32_t gsbuscfg0, gsbuscfg1, gctl, guctl, guctl1, guctl2;
    uint32_t gusb2phycfg, gusb3pipectl, gfladj, dcfg, gsnpsid;
};

struct qdwc3_ring { uint8_t *data; uint32_t size, head, tail; };

#ifdef Q1N1_GADGET_NCM
/* The NTB size limit advertised in both directions (dwNtbInMaxSize and
 * dwNtbOutMaxSize): one transfer buffer, and a multiple of the 512-byte
 * high-speed packet, which a DWC3 OUT TRB has to be. */
#define QDWC3_NCM_NTB_MAX 16384u
#define QDWC3_NCM_NTB_MIN 2048u           /* the smallest input size NCM allows */
/* All u64, appended to struct qdwc3_stats, so a host can read them through
 * the published device_stats pointer (A16-USB-REARM.md lists the offsets). */
struct qdwc3_ncm_stats {
    uint64_t requests, notifications, blocks_in, blocks_out, empty_blocks;
    uint64_t alt, alt_changes, tx_limit;
};
#endif

struct qdwc3_stats {
    uint64_t events, device_events, ep_events, setups, stalls, resets, connects, disconnects;
    uint64_t rx_bytes, tx_bytes, dropped_rx, command_failures, dma_mismatch, overflow;
    uint32_t speed, address, configured, last_command_status, last_devt;
    uint32_t restored, init_step, last_setup;
#ifdef Q1N1_GADGET_NCM
    struct qdwc3_ncm_stats ncm;
#endif
};

#ifdef Q1N1_GADGET_NCM
/* Function 1 (interfaces 2/3) as a CDC-NCM network function instead of a
 * second ACM port, so that the Mac, when it is the USB host, gets a network
 * interface as well as the proxy's serial port. This is the direction that
 * survives an A16 reset (A16-USB-REARM.md). */
struct qdwc3_ncm {
    uint8_t *tx[2];             /* IN blocks: one on the wire, the other being built */
    uint8_t *notify;            /* interrupt IN buffer */
    uint32_t tx_next;           /* which tx[] the next block is built in */
    uint32_t rx_length;         /* a received OUT block waits for release; 0 = none */
    uint32_t rx_held, alt, notify_state, filter;
    uint32_t epoch;             /* bumped whenever the function's state is forgotten */
    uint8_t input_size[8];      /* the host's SET_NTB_INPUT_SIZE payload */
};
#endif

struct qdwc3 {
    uintptr_t regs, qscratch;
    const struct qdwc3_saved *saved;
    uint8_t *arena;
    uint64_t arena_size;
    uint8_t *events;
    uint32_t event_offset;
    uint32_t ep0_state, ep0_three_stage;
    const uint8_t *ep0_buffer;
    uint32_t ep0_length;
    uint8_t *ep0_read_buffer;
    uint32_t ep0_read_length;
    struct {
        uint8_t in_progress, zlp_pending;
        uint8_t *buffer;
        uint8_t *trb;
    } ep[10];
    struct {
        struct qdwc3_ring host2device, device2host;
        uint8_t ep_in, ep_out, ready;
        uint8_t line_coding[7];
    } pipe[QDWC3_PIPES];
    uint16_t status_word;
    uint8_t string_buffer[64]; /* Each controller may have an EP0 reply pending. */
    struct qdwc3_stats stats;
#ifdef Q1N1_GADGET_NCM
    struct qdwc3_ncm ncm;
#endif
};

/* Arena must be identity-mapped DMA memory of QDWC3_ARENA_SIZE bytes. */
int qdwc3_takeover(struct qdwc3 *d, uintptr_t regs, uintptr_t qscratch,
                   const struct qdwc3_saved *saved, uint8_t *arena, uint64_t arena_size);
void qdwc3_poll(struct qdwc3 *d);
size_t qdwc3_read(struct qdwc3 *d, unsigned pipe, uint8_t *buffer, size_t count);
size_t qdwc3_write(struct qdwc3 *d, unsigned pipe, const uint8_t *buffer, size_t count);
int qdwc3_ready(const struct qdwc3 *d, unsigned pipe);
/* True while the link state machine reports Disconnected. `stats.configured`
 * cannot see a physical unplug, because device mode forces session-valid; see
 * the comment on the definition. Debounce before acting on it. */
int qdwc3_link_down(const struct qdwc3 *d);
void qdwc3_stop(struct qdwc3 *d);

#ifdef Q1N1_GADGET_NCM
/* Block-level access to the NCM function; NTB framing is ncm.c's. All of it
 * is polled: completions are taken by qdwc3_poll.
 *
 * up: configured, data interface in alternate setting 1, so the host has
 *     opened the bulk pair and will read IN blocks.
 * rx: the OUT block the host last sent, in place in the endpoint buffer, or
 *     NULL. The endpoint is not re-armed until rx_release, so the host is
 *     NAKed (never dropped) while a block is being consumed.
 * tx_buffer: where to build the next IN block (QDWC3_NCM_NTB_MAX bytes of DMA
 *     memory that is not on the wire). tx_submit sends `length` bytes of it,
 *     or returns -1 while the previous block is still in flight or the
 *     function is down; nothing is queued behind it. A block that is a whole
 *     number of 512-byte packets and shorter than tx_limit needs a ZLP the
 *     driver does not send, so the builder must not produce one.
 * tx_limit: the host's NTB input size (SET_NTB_INPUT_SIZE), clamped to ours.
 * epoch: changes whenever the host resets, reconfigures or re-selects the data
 *     interface. A block taken from rx under one epoch is gone under the next
 *     (the endpoint may already be refilling that buffer): stop walking it.
 * macs: the address this function assigns the host (its iMACAddress) and the
 *     one q1n1 uses as its own, both locally administered and fixed per
 *     controller, so two cables never share one. */
int qdwc3_ncm_up(const struct qdwc3 *d);
const uint8_t *qdwc3_ncm_rx(struct qdwc3 *d, uint32_t *length);
void qdwc3_ncm_rx_release(struct qdwc3 *d);
uint8_t *qdwc3_ncm_tx_buffer(struct qdwc3 *d);
int qdwc3_ncm_tx_submit(struct qdwc3 *d, uint32_t length);
int qdwc3_ncm_tx_busy(const struct qdwc3 *d);
uint32_t qdwc3_ncm_tx_limit(const struct qdwc3 *d);
uint32_t qdwc3_ncm_epoch(const struct qdwc3 *d);
void qdwc3_ncm_macs(uintptr_t regs, uint8_t host[6], uint8_t device[6]);
#endif

/* Platform hooks supplied by the EFI app or the host simulation. */
uint32_t qdwc3_rd(uintptr_t address);
void qdwc3_wr(uintptr_t address, uint32_t value);
void qdwc3_delay_us(uint64_t microseconds);
