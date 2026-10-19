# Innotech / NeoView Site Visit — Questions & Evidence

**For:** the Innotech (or site BMS) engineer on site
**From:** EMS water-metering gateway integration
**Date:** 5 October 2026

---

## 1. What we are trying to do

We have a working water-metering gateway (BACnet + Modbus TCP server, device
instance 1001) that publishes live meter readings and AI-generated fault
findings to a BMS. We want the BMS to read it, and we want to read BMS status
back so findings can be shown against real plant equipment.

We have completed all the work we can do from our side. We need three answers
from you.

---

## 2. Three questions

### Q1 — Which platform is the system of record?

Our scan of the office network found what appears to be **Ordr** (Automated
Logic) rather than NeoView:

| Address | What we found |
|---|---|
| `192.168.0.120` | hostname **`ENYECONTROLSBMS`**, HTTP `/` → **`/ord`** → `/login`, Modbus TCP on 502 |
| `192.168.0.40` | **Innotech Omni Controller** web interface (confirmed by page title) |
| `192.168.0.132` | Epson device — **not** Innotech (earlier note of ours was wrong) |

**Question:** Is Ordr the supervisory BMS with Innotech Omni as a subordinate
field controller? Or is NeoView present on a host we have not found?

### Q2 — What is the intended integration point?

We can read Modbus TCP at `192.168.0.120:502`, but the register block for unit
id 2 is almost entirely empty:

| Register | Value | Notes |
|---|---|---|
| 0 | `65535` | standard "not implemented" sentinel |
| 2 | `48` → `57` | the only value that changes; looks like a runtime counter |
| 4 | `24` | constant |
| 6 | `2424` | constant |

Addresses 55+ return exception `0x02` (illegal data address). Only **unit id 2**
of 248 responds.

**Question:** (a) Is there a Modbus register map for unit 2, and what do these
four registers represent? (b) Is Ordr's own API (REST/SOAP) the supported
integration route instead? If so, what are the endpoints and authentication
requirements?

### Q3 — Is UDP 47808 open on the Innotech controller?

The Omni at `192.168.0.40` answers ICMP, TCP 80, and TCP 443, but does **not**
answer a BACnet Who-Is on UDP 47808 — even to a direct unicast, not just
broadcast. Routing is fine (we reach it in 3 hops: `192.168.1.1` →
`192.168.10.1` → `192.168.0.120`).

**Question:** Is BACnet/IP enabled on that controller, and is UDP 47808
firewalled to reject requests from outside `192.168.0.x`? If BACnet is meant to
be reachable, please open it or confirm the expected firewall rule.

---

## 3. The most important question

### Q4 — Do the water meters exist in the BMS at all?

This is the one that determines whether this integration is worth continuing.

Our Modbus map contains **no meter identifiers, no totalizers, no pressure, no
battery, and no per-meter data**. It is effectively an unconfigured stub.

All real meter data today comes from a separate source — the Enye metering
MySQL database (`receive_packet`, `analy_head`, `analy_result`), which we
already read successfully.

**Question:** Are the 12 water meters represented in the BMS? If yes, where —
what are their point names / object instances? If no, then the BMS is not a
source of water-meter truth and we should integrate in the other direction
only (BMS reads our BACnet data).

---

## 4. What we need from you

1. **Platform identity** — Ordr, NeoView, or both? (Q1)
2. **Register map or API docs** for `192.168.0.120:502` unit 2 (Q2)
3. **BACnet UDP 47808** — enabled or firewalled? (Q3)
4. **Do the meters exist in the BMS?** (Q4)
5. **BACnet device instance** of any controller we should talk to, if different
   from the 2101 you quoted.
6. **Network placement** — which VLAN/subnet should the gateway sit on?

---

## 5. What we can do from our side right now

If the answer to Q4 is "yes, the meters are in the BMS," we can read them
immediately — we just need the register map or point list. If the answer is
"no," we can point the gateway at a `192.168.0.x` static address and publish
into the BMS without needing anything further from Innotech.

**We are not blocked on code.** The gateway is built, tested (285 automated
tests passing), and running. We are blocked only on the answers above.