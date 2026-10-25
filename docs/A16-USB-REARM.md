# Getting the USB network link back after an A16 reset, 2026-10-24

**Status, 2026-10-25:** on hardware the device-role link came back without a
replug three times (1 MiB round trips at 27.5–27.9 MB/s writes, 35.8 MB/s
reads), then stalled under sustained bulk writes. The stall analysis, its
fixes and the stress procedure are in
[Bulk writes stalled](#bulk-writes-stalled-2026-10-25); the fixes are offline
only. `Q1N1_GADGET_NCM` stays opt-in and the default build byte-identical.

## The problem

Every Snapintosh run starts with `a16ctl.py boot q1n1`, a full A16 reset, and
afterwards the Mac's NCM interface (`udp://enN`) stays down until the USB-C
cable is physically replugged. The A16's own CDC serial port,
`/dev/cu.usbmodemA16_Q1N1_EL2B1`, does come back by itself. Runs need NCM:
the guest console, early-stop requests and bulk transfers all ride UDP, and
CDC reads stall at multiples of 1020 bytes on this Mac
([A16-Q1N1-PROXY.md](A16-Q1N1-PROXY.md#cdc-is-still-capped-by-macos-not-by-q1n1)).

## What a reset actually does to the cable

**OBSERVED, 2026-10-24, passive IORegistry reads on the Mac, after a reset and
before any replug** (`tools/a16-link-rearm.py mac`; no request was sent to the
A16 or to a port controller):

| | |
| --- | --- |
| Mac USB-C port carrying the A16 | `DataRoleDescription = Host` on USB2 and USB3 |
| Power | the Mac draws nothing there (`IOAccessoryPowerCurrentLimits` all zero): the Mac sources |
| On the Mac's xHCI behind that port | `A16-Q1N1-EL2B`, "q1n1 A16 EL2 serial", high speed, CDC ACM interfaces only |
| The Mac's device-mode NCM interfaces | present, `status: inactive` |
| That port's `ConnectionCount` | 19860 since the Mac booted |

So after a reset **the Mac is the USB host on the cable**, and q1n1 on USB1 is
a device. The same thing was recorded on 2026-09-17: after a proxy reset and
after two power cycles the `A16-Q1N1-EL2B` device reappeared 51–55 s later
under the Mac's xHCI ([A16-DIRECT-USB-C.md](A16-DIRECT-USB-C.md); logs
`direct-only-reboot-verified.log`, `direct-only-power-cycle-{1,2}.log`).

How the serial port and the USB1 host path coexist: **they are the same
controller and connector, in alternation, never at once.**
`platform/uefi/usb-ports.c` starts USB1 in the role firmware left in GCTL
(host), dwells 1.5 s, finds no device (the Mac is a host too), stops, switches
to device mode and takes the controller over as `A16-Q1N1-EL2B`. The Mac, as
host, enumerates it. There is no second link: NCM cannot come back because
there is no Mac NCM device for q1n1 to host.

This is a different failure from the one in
[One session per replug](A16-Q1N1-PROXY.md#one-session-per-replug): that is
q1n1 resetting its xHCI (HCRST) under a Mac that is still a configured
device, which leaves the Mac's XDCI at Full speed refusing an address
(PORTSC `0x603`). After a full reset the Mac is not a device at all.

What a replug does differently is **INFERRED**, not observed at the PD level:
a fresh attach ends with the A16 as data host (DFP) while the Mac still sources
VBUS, so a data-role swap happens at attach. The A16's PPM advertises
swap-to-DFP 1 and swap-to-UFP 0; during boot services it rejected `SET_UOR`
in both directions, and the Mac's own `SWUF` request was rejected by the
partner ([A16-UCSI-REGISTERS.md](A16-UCSI-REGISTERS.md),
[A16-Q1N1-PROXY.md](A16-Q1N1-PROXY.md#the-mac-also-cannot-force-it-2026-09-17-1525z)).
Who sends the swap at attach, and why it does not happen after a reset, is not
established. `tools/a16-link-rearm.py watch` across a reset records the Mac's
side of it.

## Options, ranked

### 1. Ride the direction that survives a reset (implemented)

Make q1n1, when it is the USB device, present a CDC-NCM function next to its
ACM proxy port, so the Mac as host gets a network interface after every reset.

- **Evidence for:** the post-reset role (above) is the one this needs, and it
  came back on every recorded reset and power cycle. macOS has a host-side NCM
  driver loaded on this Mac (`com.apple.driver.usb.cdc.ncm` 5.0.0,
  `AppleUSBNCMControl` matching any interface of class 2, subclass 13, no
  vendor filter) and its CDC composite driver already binds q1n1's device
  (class 2). The UDP transport (`ncm-proxy.c`) is the same code as today's.
- **Cost in q1n1:** about 650 changed lines with comments, all behind
  `Q1N1_GADGET_NCM`, most of them in `qcom-dwc3.c` and `ncm.c`: the NCM
  function in `qcom-dwc3.c` (descriptors, class requests, alternate settings,
  notifications, block transfers), device-role hooks in `ncm.c` so NTB16
  framing and the proxy are shared rather than duplicated, and a second
  transport per device-role port in `usb-ports.c`. During a guest run,
  `q1n1_usb_ports_call_poll` also services a device-role port: its DWC3 events
  (block completions and any EP0 request macOS makes mid-run) and its UDP side
  port. Nothing waits there; a side reply that would have to wait for the wire
  is refused and counted, as on the host path. During a run the function's
  control requests are therefore answered once per `host_poll_ms` (2 ms to
  5 s in Snapintosh); a host that times out a control transfer in less than
  that would reset the device. Keep the poll at 1 s or faster on this link.
- **Cost in the tools:** discovery also finds interfaces under a USB device
  with q1n1's serial numbers (`udplink.py`), and the new interface has its own
  `enN` name. Snapintosh pins the device per run, so a run on this link is
  prepared with `--device udp://<that enN>`.
- **Throughput: not measured.** The link is USB 2.0 high speed either way
  (today's link also negotiates 480 Mb/s). Blocks are 16 KiB, one OUT transfer
  armed at a time (the Mac is NAKed, never dropped, while a block is
  consumed), and two IN buffers so one block is built while the previous is on
  the wire. Today's host link measured 29.0 MB/s writes and 32.6 MB/s reads;
  `arm` below measures this one. A shortfall would be addressed by queueing
  OUT transfers, not by changing the protocol.
- **Risk:** low and reversible. No PD, UCSI, firmware or ESP change. The ACM
  proxy port is byte-identical to today's, so the conductor keeps a lifeline
  if macOS does not bring the NCM interface up. The second ACM port (USB1's
  "console", which only ever carried a banner) is what NCM replaces in this
  build.

### 2. Do not reset between runs (exists; use more)

`tools/cycle.py` already supports consecutive runs inside one q1n1 boot:
`retire` chainloads generation N+1 after a stopped run, and
`installed-fresh --boot --after-quiesce <prefix> --generation N+1` admits the
predecessor's parked work locator. The link survives chainloads (the live NCM
state is adopted). That path ran at least once (generation 2 after v9,
2026-10-20).

What `--generation 1` after a cold boot protects (`installed_fresh.admit`,
`launch.admit_records`): a GPU nobody has prepared since power-on (the warm
path instead requires a graceful quiesce record), no resident guest work
area, secondaries known to be in PSCI CPU_OFF, no host exceptions or SError
since the stage started, and device state (NVMe queues, Wi-Fi firmware, the
display controller) that only a guest of *this* q1n1 boot has touched. A warm
re-arm needs the predecessor's preserve, quiesce and retire records; it cannot
follow a run that ended in a hang, an unrecovered GPU, or an exception, and
those still need a reset. So this lowers how often the problem occurs; option 1
removes it.

### 3. PD Hard Reset through UCSI `CONNECTOR_RESET` (not implemented)

- **Semantics:** in UCSI ≥ 2.0 (this PPM reports `0x0210`) a Hard Reset is
  `CONNECTOR_RESET` (0x03) with **bit 23 clear**; bit 23 set means *Data
  Reset* (USB4) since 2.0, and meant Hard Reset only in 1.0 (Linux
  `ucsi_reset_connector()`, `UCSI_CONNECTOR_RESET_DATA_VER_2_0`). Connector 1:
  `0x00010003`.
- **Why it probably does not help here:** a Hard Reset returns both ends to
  their default data roles, and with the Mac sourcing VBUS the default is the
  Mac as host, the state we are already in after a reset. It would only help
  if the A16's PD firmware swaps to DFP after a Hard Reset as it does after a
  physical attach, which is unknown, and in boot services this PPM has refused
  both swap directions.
- **How to test it with existing code:** the boot window's UCSI console
  (`a16ctl.py boot hold`, then `a16ucsi.py raw 0x10003`) sends any CONTROL
  value, but the boot window exists only on USB0, so it needs a second cable
  from the Mac (or the dock) to the A16's other port. Post-EBS it needs a
  CONTROL write path (GLINK opcode `0x91`, register `0x20108`) added to
  `pmic-router.c` next to its read-only allow-list.
- **Risk:** VBUS drops for the duration of the reset, and the reset also drops
  the CDC link the request came over, so it is fire-and-forget; worst case is
  a replug. Ranked below option 2 because it addresses a role swap that option
  1 does not need.

### 4. Detect the wedge and recover in q1n1 (detection implemented in the tool)

The port manager already recovers what can be recovered without PD: a failed
host attempt falls back to device mode, which is why the CDC port returns. With
option 1 that fallback now also yields NCM. What it cannot fix is the Mac
staying a device but wedged (Full speed, no address). `a16-link-rearm.py a16`
names that state from q1n1's own records (USB role host, `last_error` -3,
"Address Device failed"; with `--portsc`, PORTSC speed Full). Automatic
recovery from it would need option 3. It has not been observed after a reset.

### 5. A Mac-side disconnect without admin rights (not available)

The AppleHPM user client that could ask the Mac's port controller for a
disconnect or a role swap needs administrator access (`mac-typec-device.cpp`
fails with "administrator access required" otherwise), and its `SWUF` was
rejected by the A16 anyway. IOUSBHost re-enumeration resets the Mac's host
port, which does not change the PD data role. Rejected under the no-sudo,
no-persistent-setting constraint.

## The implementation (option 1)

Build: `make -f platform/uefi/Makefile OUT=build/uefi-gadget-ncm
Q1N1_DEFINES=-DQ1N1_GADGET_NCM`. Without the define nothing changes: the
default stages rebuild byte-identical (`q1n1-stage.bin` `56f57510…`,
`q1n1-stage-alt.bin` `5d6f7df0…`, Snapintosh's pinned pair), and `q1n1.efi`
differs only in the COFF link timestamp.

| Piece | What the define adds |
| --- | --- |
| `qcom-dwc3.c` | Function 1 is CDC-NCM 1.0 on the second ACM port's endpoints (0x83 interrupt, 0x04/0x84 bulk), so the endpoint and TX FIFO assignment proven on this controller is unchanged. NTB16 only, 16 KiB blocks both ways, 4-byte alignment. Requests: GET_NTB_PARAMETERS, GET/SET_NTB_INPUT_SIZE, GET/SET_NTB_FORMAT (16-bit only), SET_CRC_MODE (none only), SET_ETHERNET_PACKET_FILTER; everything else stalls. GET_INTERFACE/SET_INTERFACE on the data interface. CONNECTION_SPEED_CHANGE then NETWORK_CONNECTION on alternate setting 1. iMACAddress string. |
| `ncm.c` | `ncm_open_gadget` binds a `struct ncm` to the function; `ncm_send`, `ncm_queue/flush`, `ncm_send_side` and `ncm_receive` move NTB16 blocks through it with the same framing code as the host path. Blocks are never a whole number of 512-byte packets (four bytes of padding before the NDP), so no ZLP is needed. |
| `usb-ports.c` | A device-role port serves the proxy on ACM and on UDP over NCM; one request stays on the transport it started on, each transport negotiates its own checksums. Announces on each opening of the data interface. |
| `entry.S` | Stage feature bit 1, so tools can tell a gadget-NCM stage; bit 3 and the counter table's address at image offset 64. |

**The shared USB state is the default build's, byte for byte.** A stage
adopts the running payload's `struct q1n1_usb_ports` in place, and before any
upload the reader compares the stage's handoff contract (magic, version, size)
with the payload's whenever a port is in the host role
(`q1n1proxy.check_stage_transport`). So nothing the define adds lives in
`struct qdwc3`, `struct ncm` or `struct q1n1_usb_port`: the function's state
and counters (`qcom-dwc3.c`), the link's binding and receive walk (`ncm.c`)
and the per-port flags (`usb-ports.c`) are stage memory, found by controller
or link. `tools/test-q1n1-stage-contract.py` checks it on the built images.
The first two gadget stages (`770188d9`, `ef8f8cd4`) did change it (version 2,
size `0x6280` and `0x6320` against `0x6150`); the second was refused from the
installed payload, and the first had been armed only because no port happened
to be in its host role when the reader looked (an idle port alternates).
`a16-link-rearm.py arm` now makes the same comparison itself every time,
whatever the roles, before anything is uploaded.
| `main.c` | The status panel shows the device-role NCM state. |

Addresses are fixed and locally administered, `02:51:31:4e:48:0N` for the
Mac's interface and `02:51:31:4e:44:0N` for q1n1 (N = controller), so macOS
keeps one interface name per A16 port; q1n1 answers on `fe80::4919` as before.

Host side: `udplink.find_interfaces()` also returns NCM interfaces below a USB
device whose serial is `A16-Q1N1-EL2` or `A16-Q1N1-EL2B` (never a phone or an
adapter), and `tools/a16-link-rearm.py` is the experiment tool below.

### Layout of the published counters

`bootinfo.usb_ports` and every record it points to are the default build's.
The NCM function's counters are `struct qdwc3_ncm_stats`, 16 u64: requests,
notifications, blocks_in, blocks_out, empty_blocks, alt, alt_changes,
tx_limit, out_not_ready, rx_chained, rx_trailing, tx_waits, tx_wait_max_us,
service_gap_max_us, service_gaps_20ms, probes_deferred. A stage with feature
bit 3 names `qdwc3_ncm_table` at image offset 64 (magic `Q1NCMTB1`, then its
address): magic, version 1, count, then a (controller base, counters address)
pair per function. `ncm_stats` and `proxy_stats` of a device-role port
describe the NCM link. `tools/a16-link-rearm.py a16` reads all of this by the
feature bits, never by computed offsets; it still reads the two retired
layouts (bits 1 and 2 without 3: 8 or 16 u64 appended to `struct
qdwc3_stats`) so older records stay readable.

### Validation

| Check | Result |
| --- | --- |
| `python3 tools/test-q1n1-stage-contract.py` | PASS: 21 (both builds: every image's handoff contract equals the default q1n1.efi's, the shared structs' layout field by field, and the reader's own check with a host-role port; it fails on `e9d561f` and `ee8c00d`) |
| `python3 tools/test-q1n1-ports.py` | PASS: 140 port checks (default), 169 (gadget build), 118 device-role NCM checks, 25 DWC3 checks, stop, boot choice, 10 discovery tests |
| `python3 tools/test-q1n1-udp-loss.py` | PASS: 11 (datagram loss and recovery against a model of the target's request loop) |
| `python3 tools/test-q1n1-xhci.py` | PASS: 128 checks, default and gadget builds |
| `python3 tools/test-q1n1-ncmproxy.py` | PASS: 80 |
| `python3 tools/test-q1n1-proxy.py` | PASS: 82 (one earlier run failed with a pty byte desync; its sources are untouched here and six reruns passed) |
| `python3 tools/test-a16-link-rearm.py` | PASS: 25 |
| `python3 tools/test-uefi.py --el 2 --proxy --build-dir build/uefi-gadget-ncm` | PASS: 89 (QEMU does not model the DWC3; this proves the build boots, takes EL2 and chainloads both slots) |
| Default stage pair vs. the unmodified tree | byte-identical |

`tools/test-q1n1-gadget-ncm.c` drives the real `qcom-dwc3.c`, `ncm.c` and
`ncm-proxy.c` as a host would: enumeration and descriptor walk, class
requests, notifications, a neighbour solicitation and proxy datagrams in one
block, multi-block replies within the host's input size, anti-ZLP padding,
side-port replies inside a P_CALL that never wait, a data interface
re-selected while a block is half walked (the test fails without the epoch
check in `ncm_receive`), malformed blocks, a lone ZLP, an illegal input size,
alternate setting 0, and a bus reset. It models
the NCM 1.0 protocol, not macOS's driver; whether macOS binds this function is
what the hardware session establishes.

## Hardware procedure (one session)

Prerequisites: AC or at least 50 % battery; no other hardware client; the
direct cable as it is today. Run from the q1n1 checkout that holds this branch,
with records under `build/a16-link-rearm/` (ignored).

1. **Build and check** (offline):

   ```sh
   make -f platform/uefi/Makefile OUT=build/uefi-gadget-ncm Q1N1_DEFINES=-DQ1N1_GADGET_NCM stage
   shasum -a 256 build/uefi-gadget-ncm/q1n1-stage.bin build/uefi-gadget-ncm/q1n1-stage-alt.bin
   python3 tools/test-q1n1-ports.py && python3 tools/test-a16-link-rearm.py
   ```

2. **Watch the Mac side** in a second terminal (read-only, keep it running):

   ```sh
   python3 tools/a16-link-rearm.py watch --seconds 600 --record build/a16-link-rearm/watch.json
   ```

3. **Reset as today**, no replug:

   ```sh
   python3 tools/a16ctl.py boot q1n1
   python3 tools/a16-link-rearm.py mac --record build/a16-link-rearm/after-reset.json
   ```

   Expected: `--auto reached the EL2 proxy directly on /dev/cu.usbmodemA16_Q1N1_EL2B1`,
   and `mac` classifies `mac-is-host ... A16-Q1N1-EL2B at high speed, without an NCM function`.
   If it says `ncm-host` instead, the reset kept the A16 as host this time:
   record that, it is evidence about the role swap, and the run can proceed
   as usual (`arm` would refuse, by design).

4. **Read the A16's side** (read-only):

   ```sh
   python3 tools/a16-link-rearm.py a16 --device /dev/cu.usbmodemA16_Q1N1_EL2B1 --portsc \
       --record build/a16-link-rearm/gen0.json
   ```

   Expected: generation 0, EL2, USB1 `role: device`, `connected: 1`. A USB1
   `last_error` of -3 would mean the Mac was a device and wedged; report it.

5. **Arm**, the one change:

   ```sh
   python3 tools/a16-link-rearm.py arm build/uefi-gadget-ncm/q1n1-stage.bin \
       --device /dev/cu.usbmodemA16_Q1N1_EL2B1 --yes --record build/a16-link-rearm/arm-1.json
   ```

   It refuses without AC or 50 %, and refuses a stage whose USB handoff
   contract differs from the running payload's. If the generation-0 payload
   has no live battery path it says `battery unreadable`; check the battery
   yourself and rerun with `--battery-ok`. Success prints
   `PASS: q1n1 NCM link up on udp://enX`, after a verified upload over CDC
   (about 690 KB, read back in 1008-byte pieces), the new interface active,
   generation 1 answering at its slot over UDP, 200 NOPs, and a 1 MiB heap
   round trip whose original bytes are restored; its write and read MB/s are
   in the record.

6. **The goal: a second reset, still no replug.** Repeat steps 3 to 5 (record
   to `arm-2.json`). Two consecutive PASSes establish the fix on this cable.

7. Optionally read the counters over the new link:
   `python3 tools/a16-link-rearm.py a16 --device udp://enX --record build/a16-link-rearm/gen1.json`
   (`gadget.alt` 1, `notifications` 2, `tx_limit` as macOS set it).

**Using it for a Snapintosh run** after a PASS: the A16 is at generation 1,
so prepare and boot with `--device udp://enX` and do **not** chainload the
default stage (run scripts: `CHAINLOAD=0`): the default stage has no NCM
function and would take the link away. For `cycle.py retire` to keep the link,
point the local `tools/cycle_lib/pins.json` `stages` at the gadget pair.

**If something fails:**

| Where | State | Do |
| --- | --- | --- |
| `arm` refuses | nothing changed | fix the cause it names |
| upload does not verify | the old stage still runs, nothing jumped | rerun, or stop here |
| stage runs, no NCM interface within 90 s | the new stage still serves ACM on `EL2B` | `a16-link-rearm.py a16 --device /dev/cu.usbmodemA16_Q1N1_EL2B1` (gadget `requests` and `alt` say whether macOS bound it), keep the record, then `a16ctl.py boot q1n1` |
| nothing answers | stage fault: it halts or resets into the boot window (BootNext is armed) | wait 60 s for `EL2B`; else the power button; worst case a replug, which is today's state |

## Bulk writes stalled, 2026-10-25

**Hardware (reported by the conductor):** a desktop boot over the gadget link
(generation 1, stage `770188d9`) had 77 uploads (18,989,568 bytes) acknowledged,
the last a 13,942,784-byte write, and the next write's reply never came
(`udplink.py` `TimeoutError: expected 1 bytes, got 0`). At idle, an 8 MiB write
and read back passed in 0.8 s, then a 2 MiB write timed out after 16.8 s; a
fresh connect worked again. No q1n1 counter showed a drop.

### What the evidence says

- **The target side of the symptom (VERIFIED from source):** `memory_write`
  in `q1n1-proxy.c` reads its data with a 5 s idle timeout, and on expiry gives
  the request up without a reply (`q1n1_proxy_stats.timeouts`). A host write
  whose reply never comes, after which the stream works again, is exactly a
  datagram lost on the way to the A16. The link then recovers by itself, as
  seen.
- **The Mac's host NCM driver (OBSERVED in the KDK's AppleUSBNCM, study only,
  nothing committed):** `AppleUSBNCMData::transmitZLP()` exists, so it ends a
  block of whole packets with a ZLP; `transmitRecord` and `transmitZLP` give
  every OUT write a 5000 ms completion timeout; IN reads have none; output is
  a pull model from the interface queue. So the driver itself only drops an
  NTB that q1n1 leaves un-armed for 5 s, which no q1n1 path does. A loss has
  to happen before the driver: in the interface queue, which drops silently
  once held up long enough. That is the mechanism this repository already
  recorded for the host-role link, where the cure for large writes was
  "removing the discovery stalls, during which the Mac's queue overflowed"
  ([A16-Q1N1-PROXY.md](A16-Q1N1-PROXY.md#what-was-actually-limiting)).
- **What holds q1n1 up (VERIFIED from source, reproduced in the port model):**
  with only USB1 cabled, USB0 alternates host and device attempts whenever the
  live link has carried no traffic for 2 s. Each attempt holds the whole loop
  for 50–150 ms: a fixed 50 ms role settle, then the device takeover's halts,
  soft reset and another 50 ms, or the xHCI reset. A write that starts after a
  pause of 2 s or more (an interactive step, a pause between boot uploads) can
  land in one, and while it lasts nothing takes the Mac's blocks. In
  `tools/test-q1n1-usb-ports.c` a quiet live link sees 4 such attempts in 10 s,
  each holding a poll for 50 ms under the old rule, and none under the new one.
- **A q1n1 path that loses data silently (VERIFIED, reproduced):** an OUT
  transfer that carries a second NTB (a host that sends no ZLP after a block
  of whole packets) had its second NTB ignored, with no counter. macOS sends
  the ZLP, so this is unlikely to be the hardware cause, but
  `tools/test-q1n1-gadget-ncm.c` shows the loss without the fix.
- **The host's reliability model (VERIFIED from source):** UDP, no sequence
  numbers, no retransmission, and `q1n1proxy.Link` had no recovery: any single
  lost datagram became a timeout.
- **Not found:** a TX block overwritten while in flight, or a completion
  missed while servicing. The two transmit buffers alternate and a block is
  built only in the one not on the wire; the receive buffer is re-armed only
  after its block is consumed, and an epoch drops a block the host reset.

Which of the first two caused the hardware failure is **INFERRED**, not
proven: the counters below exist to decide it in one session.

### Fixes (all behind `Q1N1_GADGET_NCM`, default build byte-identical)

1. **A live link is not held up for discovery** (`usb-ports.c`). For a
   stage's first 30 s the default rule holds, so cables plugged in at start
   are found. After that, an idle port makes no role attempt while any other
   port is connected. A port whose own link drops still goes through its
   disconnect handling. A cable plugged in later beside a live link is found
   when that link goes away, when a new stage starts, or on request: the
   existing `q1n1_xhci_retry` P_CALL (`q1n1_usb_ports_refresh`) reopens the
   30 s window.
2. **Every NTB in an OUT transfer is walked** (`ncm.c`); bytes after the last
   one are counted (`rx_trailing`), never silently dropped.
3. **Counters for what no counter saw** (`qdwc3_ncm_stats`, in the table
   stage feature bit 3 names): `service_gap_max_us` and `service_gaps_20ms` (how long the live port
   went unserviced), `out_not_ready` (the Mac had a block and nothing was
   armed), `tx_waits` and `tx_wait_max_us` (an IN block waited for the
   previous one), `rx_chained`, `rx_trailing`, `probes_deferred`. Plus the
   target's own `proxy.timeouts`, now read by the `a16` view.
4. **Host recovery, opt-in** (`q1n1proxy.Link`, `Q1N1_UDP_RETRIES=N`, datagram
   transports only). A memread or memwrite that times out is resynchronised
   and repeated up to N times; both are idempotent, P_ requests never are
   repeated. Resynchronising sends zero filler (never a request magic) to
   complete a write the target is still reading inside the range being
   written, so it answers ST_XFRERR at once; without that answer it waits out
   the target's 5 s idle timeout. It then drains, and renews the negotiation
   with a NOP, which also proves the stream is in step. A failed resync is not
   repeated. `tools/test-q1n1-udp-loss.py` drives this against a model of the
   target's request loop with datagrams lost in each direction: a write, a
   reply, read data, a request header, sentinel mode, and the bounds.
   Default off: the stress below qualifies the link with it off.

### Stress procedure (one session, after `arm`)

The new stage is `build/uefi-gadget-ncm/q1n1-stage.bin` `0e220a1f…` /
`q1n1-stage-alt.bin` `da9d392c…` (691,176 bytes each, stage features 11,
handoff contract version 1, size `0x6150`, the installed payload's). It can be
armed from generation 0 of the installed payload; `ef8f8cd4` cannot.

1. The stall is not reproduced on the old stage first: `arm` now refuses
   `770188d9`/`2b70984b` and `ef8f8cd4`/`a2be6852`, whose handoff layout is
   not the installed payload's. The two earlier failures (the 13,942,784-byte
   upload and the idle 2 MiB write) are the reference.
2. Reset and `arm` the new stage over the CDC port exactly as in the
   procedure above (`a16ctl.py boot q1n1`, then `arm ... --device
   /dev/cu.usbmodemA16_Q1N1_EL2B1`; do not arm over a running gadget link,
   whose interface stays active for a moment after the jump). Then **wait
   35 s** so the start-up discovery window has closed.
3. Run, with nothing else using the A16:

   ```sh
   python3 tools/a16-link-rearm.py stress --device udp://enX --yes \
       --record build/a16-link-rearm/stress-1.json
   ```

   64 MiB in 8 MiB, 2 MiB, 13,942,784-byte, 1 MiB, 4 KiB and 300,000-byte
   writes, each read back and compared, with 3 s of silence before every third
   write (the pause that used to land in a discovery stall), into the largest
   firmware-reported free span outside q1n1's regions. About 20 s.

**Pass:** `PASS: ... no timeout`, every transfer `ok`, and in the printed deltas:

| Counter | Expected | Meaning if not |
| --- | --- | --- |
| `proxy.timeouts` | 0 | a write lost data on the way to the A16 |
| `usb1.gadget.service_gaps_20ms` | 0 | something still holds the loop up; `service_gap_max_us` says how long |
| `usb1.gadget.probes_deferred` | 1 or more | the idle port would have probed and did not |
| `usb1.gadget.rx_trailing`, `usb1.ncm.malformed`, `bad_*`, `send_failures` | 0 | framing trouble |
| `usb1.gadget.rx_chained` | 0 expected (macOS sends ZLPs) | nonzero: blocks shared transfers, now walked |
| `usb1.gadget.out_not_ready`, `tx_waits` | any | flow control; `tx_wait_max_us` well under 1 s |
| `mac.drop`, `mac.oerrs` | 0 | the Mac dropped on its side |

Write and read MB/s are in the summary. Today's figures for the same link are
27.5–27.9 MB/s writes and 35.8 MB/s reads in 1 MiB round trips.

**If it fails:** the record holds per-transfer timings and both sides'
counters. A timeout with `service_gaps_20ms` above zero says something else
still stalls the loop; one with gaps at zero and `mac.drop` or the queue text
(`netstat -qq`) showing drops says the Mac drops even without a stall, and the
next step is `Q1N1_UDP_RETRIES=2` for runs (rerun with `--retries 2` to
confirm it recovers) while the cause is chased. The link recovers by itself
after a timeout (the target gives up after 5 s); the tool resynchronises and
continues, stopping after three failures.

For a desktop boot on this link: chainload the new stage (the run scripts with
`CHAINLOAD=0` after `arm`), and set `Q1N1_UDP_RETRIES=2` in the environment of
`cycle.py` only after the stress has passed with it off, so a residual loss
costs a retry rather than the boot.

## Installing it on the ESP (optional; needs the owner's authorisation)

Arming costs one chainload over CDC after each reset. Installing the gadget
build's `q1n1.efi` as the ESP payload would make generation 0 itself offer NCM
after a reset. That is a persistent write to the A16's ESP: **do not do it
without the owner's explicit go-ahead**, and only after step 6 has passed.

With the guarded installer, in an elevated PowerShell on the A16, with
`build/uefi-gadget-ncm/q1n1.efi` copied as `q1n1-usb.efi` next to
`tools/install-a16-q1n1-boot.ps1` in the existing bring-up directory:

```powershell
$hash = (Get-FileHash .\q1n1-usb.efi -Algorithm SHA256).Hash.ToLower()
.\install-a16-q1n1-boot.ps1 -PayloadSHA256 $hash -BootEntry '{<boot-entry-guid>}' -Update
```

The installer refuses unless the hash matches the staged file, the machine
and ESP identity match, every recorded ESP file and the known-good
`\EFI\q1n1\q1n1.efi` are unchanged, and Windows is first in the firmware boot
order. It backs the replaced payload up as `q1n1-usb-<first 12 hex>.efi`,
verifies the written payload and `\startup.nsh`, and re-checks that the
Windows boot manager and boot order did not change. To roll back, rerun it
with `-PayloadFile` naming that backup, its hash, and `-Update`. A failed
payload costs a power cycle: `startup.nsh` falls back to Windows. After an
install, `--generation 1` runs still chainload once; the difference is that
the link already exists at generation 0.

## Open questions

- Whether macOS binds this NCM function and brings the interface up with an
  IPv6 link-local address without a network service being configured
  (`mac` reports `link_local` per interface). macOS normally adds a service
  for a new Ethernet interface itself; if it does not, that is a Mac network
  setting for the owner, not something the tools change.
- The PD sequence of a replug versus a reset (option 3's premise).
- Throughput of the device-role link.
