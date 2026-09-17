# BMS Point List — Request Form

Fill this in and return it (or a spreadsheet with the same columns) to finalize
`water-gateway/config/meters.yaml`. Nothing else in the gateway changes — once
these values come back, the map is swapped in and the gateway goes live.

Gateway details for reference:

- Modbus TCP server port: **502** (default), one unit id per meter (12 units).
- BACnet/IP server port: **47808**, one device instance (default **1001**).
- 12 meters, 9 points each (listed below).

---

## 1. Per-meter point table (repeat for each of the 12 meters)

Meter ID: ______________     Modbus unit id (1–247): ______________

| Point | Modbus address (register or coil) | Data type on wire | Scale on wire | Byte order / word order | R/W | BACnet object type | BACnet instance | BMS tag (optional) |
|-------|-----------------------------------|-------------------|---------------|--------------------------|-----|--------------------|-----------------|--------------------|
| totalizer (m³) | ? | ? | ? | ? | R | ? | ? | |
| pressure (kg/cm²) | ? | ? | ? | ? | R | ? | ? | |
| reverse (m³) | ? | ? | ? | ? | R | ? | ? | |
| pulse1 (L) | ? | ? | ? | ? | R | ? | ? | |
| pulse2 (L) | ? | ? | ? | ? | R | ? | ? | |
| battery (V) | ? | ? | ? | ? | R | ? | ? | |
| signal (dBm) | ? | ? | ? | ? | R | ? | ? | |
| online (0/1) | ? | ? | ? | ? | R | ? | ? | |
| alarm (0/1) | ? | ? | ? | ? | R | ? | ? | |

**Example row (filled):**

| Point | Modbus address | Data type | Scale | Byte/word order | R/W | BACnet type | BACnet instance | BMS tag |
|-------|----------------|-----------|-------|-----------------|-----|-------------|-----------------|---------|
| totalizer | 40001 | float32 | 1.0 | big/big | R | AI | 1 | METER01-CC1-TTL |

Notes:

- **Modbus address convention:** are the addresses above 1-based readouts (e.g.
  `40001` = holding register) or 0-based raw PDU addresses (e.g. `0`)? Tell us
  once; it applies to all rows.
- If a point is not exposed by the BMS, mark the Modbus fields **N/A** and we
  skip it on that protocol only.
- If a point should be read at a **different numeric scale** on the wire
  (e.g. battery in millivolts), put the wire scale there.

---

## 2. Protocol-level answers (one-time)

1. **Modbus unit scheme:** one unit id per meter (current, 1–12) — confirm, or
   specify a single shared unit id or a different numbering.
2. **BACnet device instance:** is 1001 OK, or assign a number for our gateway
   device on the BMS network?
3. **Ports:** 502 (Modbus) and 47808 (BACnet/IP) usable, or are we assigned
   different ports?

---

## 3. Rule decisions (owner confirmations)

1. **Alarm point (alarm 0/1):** what makes it active?
   - Source to use: `analy_result.alarm`? `analy_file.alarm`?
     `minin_water_guard_alarm`? A combination?
   - Which codes/conditions are "actionable" (vs informational)?
2. **Pressure unit label:** we map kg/cm² to BACnet `bars` (1 kg/cm² ≈ 0.98 bar).
   Confirm acceptable, or specify the exact label.
3. **Signal (dBm):** BACnet has no dBm unit — we send the dBm number with
   `noUnits`. Confirm acceptable.
4. **Invalid/unknown value on the wire:** when battery/signal/etc. is missing,
   what should the BMS see? (e.g. 0, -1, -999, NaN float, or leave prior value?)

---

## Return path

- Reply with this document filled (or a per-meter row per spreadsheet), plus the
  answers to sections 2 and 3.
- We convert it directly into `config/meters.yaml` and validate on startup —
  no code changes, no restart risk beyond dropping in the new map.