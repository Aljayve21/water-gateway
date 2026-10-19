# Controls / BMS team - data request (Neoview point config + meter register map)

**For:** the Innotech / Neoview controls engineer
**From:** EMS water-metering gateway integration
**Date:** 19 October 2026
**Re:** finish BMS point validation, and prepare (later) write-back

---

## 0. Where we are

We read the Neoview BMS over **oBIX** (read-only) from `192.168.0.120` - **141 points**
across two devices (`EnyecontrolsBMS_IO` and `iEM3265_0020343063`). We validate every
reading against an expected unit and range and raise a finding when a value is missing,
mis-scaled, out of range, or stale.

**We can now read Neoview's own point facets** (`min` / `max` / `precision` / `unit`)
over oBIX, so your point configuration is the source of truth - not our guesses. A probe
on 19 Oct found facets on **114 of 141** points; the other 27 are the boolean/occupancy
points in section 2.

That means the quality of the alarm depends almost entirely on your point config. Below
are the specific gaps, in priority order. **Snapshots in this document are from
`2026-10-19T01:43:48+00:00`.**

---

## 1. Engineering ranges - the highest-value fix

Our validator trusts Neoview's own range. Where it is wrong it creates a false alarm;
where it is unset we cannot validate the point at all.

### 1a. Range appears to be a placeholder (`0 - 100` on a non-percent point)

These are not unit-aware. On the `B: *_AIxx` set the range is a generic `0 - 100` and has
nothing to do with the unit. **Voltage L1-L2 below is causing a live false CRITICAL** - it
reads ~244 V against a 0-100 range.

| Point (meter/point) | Unit | Range now | Proposed / needed |
|---|---|---|---|
| `bms_enyecontrols/b_2f_co2_reading_ai34` | parts per million | 0.0 - 100.0 | 0.0 - 2000.0  (match enyecontrolsbms_io_2f_co2_reading) |
| `bms_enyecontrols/b_2f_iaq_pm_reading_ai35` | micrograms per cubic meter | 0.0 - 100.0 | 0 - 500 ug/m3 |
| `bms_enyecontrols/b_3f_co2_reading_ai38` | parts per million | 0.0 - 100.0 | 0.0 - 2000.0  (match enyecontrolsbms_io_3f_co2_reading) |
| `bms_enyecontrols/b_3f_iaq_pm_reading_ai39` | micrograms per cubic meter | 0.0 - 100.0 | 0 - 500 ug/m3 |
| `bms_enyecontrols/b_3f_temp_reading_ai37` | celsius | 0.0 - 100.0 | -40.0 - 60.0  (match enyecontrolsbms_io_3f_temp_reading) |
| `bms_enyecontrols/b_gf_co2_reading_ai26` | parts per million | 0.0 - 100.0 | 0.0 - 2000.0  (match enyecontrolsbms_io_gf_co2_reading) |
| `bms_enyecontrols/b_gf_iaq_pm_reading_ai27` | micrograms per cubic meter | 0.0 - 100.0 | 0 - 500 ug/m3 |
| `bms_enyecontrols/b_mezz_conf_temp_reading_ai31` | celsius | 0.0 - 100.0 | -40.0 - 60.0  (match enyecontrolsbms_io_mezz_conf_temp_reading) |
| `bms_enyecontrols/b_mezz_tr_temp_reading_ai29` | celsius | 0.0 - 100.0 | -40.0 - 60.0  (match enyecontrolsbms_io_mezz_tr_temp_reading) |
| `bms_enyecontrols/enyecontrolsbms_io_voltage_l1_l2` | volt | 0.0 - 100.0 | 0 - 500 V (L-L) / 0 - 300 V (L-N) |

### 1b. Range never set (`min == max = 0`)

39 points report `min = max = 0`. We treat that as "no bound" (it must not
flag every reading), but it means the point is completely unvalidated. Please set a real
engineering range.

| Point (meter/point) | Unit | Range now | Proposed / needed |
|---|---|---|---|
| `bms_enyecontrols/2f_btu_reading_ai17` | kilobtus per hour | 0.0 - 0.0 | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/2f_power_m_reading_ai23` | null | 0.0 - 0.0 | set a real range |
| `bms_enyecontrols/2f_rh_reading_ai52` | percent | 0.0 - 0.0 | 0.0 - 100.0  (match enyecontrolsbms_io_2f_rh_reading) |
| `bms_enyecontrols/2f_temp_reading_ai53` | celsius | 0.0 - 0.0 | -40.0 - 60.0  (match enyecontrolsbms_io_2f_temp_reading) |
| `bms_enyecontrols/2f_water_m_reading_ai20` | null | 0.0 - 0.0 | set a real range |
| `bms_enyecontrols/3f_btu_reading_ai18` | kilobtus per hour | 0.0 - 0.0 | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/3f_power_m_reading_output_ai24` | null | 0.0 - 0.0 | set a real range |
| `bms_enyecontrols/3f_water_m_reading_ai21` | null | 0.0 - 0.0 | set a real range |
| `bms_enyecontrols/bm_co_reading_ai14` | parts per million | 0.0 - 0.0 | 0.0 - 250.0  (match enyecontrolsbms_io_bm_co_reading) |
| `bms_enyecontrols/current_phase_3_ai8` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/e_current2_ai44` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/e_current3_ai45` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/e_current_average_ai50` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/ec_current1_ai40` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/ec_current2_ai41` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/ec_current3_ai42` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/ec_current_average_ai49` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/enye_current1_ai43` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/gf_btu_reading_ai16` | kilobtus per hour | 0.0 - 0.0 | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/gf_power_m_reading_ai22` | kilowatt hour | 0.0 - 0.0 | 0 - open (cumulative total) |
| `bms_enyecontrols/gf_rh_reading_ai12` | percent | 0.0 - 0.0 | 0.0 - 100.0  (match enyecontrolsbms_io_gf_rh_reading) |
| `bms_enyecontrols/gf_temperature_reading_ai13` | celsius | 0.0 - 0.0 | -40 - 60 C |
| `bms_enyecontrols/gf_water_m_reading_ai19` | cubic meter | 0.0 - 0.0 | 0 - open (cumulative total) |
| `bms_enyecontrols/pm_current_average_ai7` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/pm_current_phase_1_ai2` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/pm_current_phase_2_ai9` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/pm_frequency_ai10` | hertz | 0.0 - 0.0 | 45 - 65 Hz |
| `bms_enyecontrols/pm_power_factor_total_ai3` | null | 0.0 - 0.0 | set a real range |
| `bms_enyecontrols/pm_voltage_average_l_l_ai6` | volt | 0.0 - 0.0 | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/pm_voltage_l1_l2_ai1` | volt | 0.0 - 0.0 | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/pm_voltage_l2_l3_ai4` | volt | 0.0 - 0.0 | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/pm_voltage_l3_l1_ai5` | volt | 0.0 - 0.0 | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/t_current1_output_ai46` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/t_current2_ai47` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/t_current3_ai48` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/t_current_average_ai51` | ampere | 0.0 - 0.0 | 0 - <meter rating> A |
| `bms_enyecontrols/total_energy_kwh_ai11` | kilowatt hour | 0.0 - 0.0 | 0 - open (cumulative total) |
| `bms_enyecontrols/total_m3_ai25` | cubic meter | 0.0 - 0.0 | 0 - open (cumulative total) |
| `bms_enyecontrols/total_m3_ai32` | cubic meters per hour | 0.0 - 0.0 | 0 - 100 m3/h |

### 1c. No range at all (both bounds unset)

| Point (meter/point) | Unit | Range now | Proposed / needed |
|---|---|---|---|
| `bms_enyecontrols/enyecontrolsbms_io_2f_btu_reading` | kilobtus per hour | not set | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/enyecontrolsbms_io_2f_power_consumption` | kilowatt hour | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_2f_water_consumption` | cubic meter | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_3f_btu_reading` | kilobtus per hour | not set | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/enyecontrolsbms_io_3f_power_consumption` | kilowatt hour | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_3f_water_consumption` | cubic meter | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_ec_current_average` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_ec_current_l1` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_ec_current_l2` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_ec_current_l3` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_ec_total_consumption` | kilowatt hour | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_enye_current_average` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_enye_current_l1` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_enye_current_l2` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_enye_current_l3` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_frequency` | hertz | not set | 45 - 65 Hz |
| `bms_enyecontrols/enyecontrolsbms_io_gf_btu_reading` | kilobtus per hour | not set | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/enyecontrolsbms_io_gf_water_consumption` | cubic meter | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_main_btu_reading` | kilobtus per hour | not set | CONFIRM SCALE (values seen ~1e6-7e6) |
| `bms_enyecontrols/enyecontrolsbms_io_power_factor` | null | not set | set a real range |
| `bms_enyecontrols/enyecontrolsbms_io_tihik_current_average` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_tihik_current_l1` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_tihik_current_l2` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_tihik_current_l3` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_total_current_average` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_total_current_l1` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_total_current_l2` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_total_current_l3` | ampere | not set | 0 - <meter rating> A |
| `bms_enyecontrols/enyecontrolsbms_io_total_electrical_consumption` | kilowatt hour | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_total_water_consumption` | cubic meter | not set | 0 - open (cumulative total) |
| `bms_enyecontrols/enyecontrolsbms_io_voltage_average` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/enyecontrolsbms_io_voltage_l2_l3` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_enyecontrols/enyecontrolsbms_io_voltage_l3_l1` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_iem3265_0020343063/ai07_current_phase_1` | ampere | not set | 0 - <meter rating> A |
| `bms_iem3265_0020343063/ai08_current_phase_2` | ampere | not set | 0 - <meter rating> A |
| `bms_iem3265_0020343063/ai09_current_phase_3` | ampere | not set | 0 - <meter rating> A |
| `bms_iem3265_0020343063/ai11_voltage_l1_l2` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_iem3265_0020343063/ai12_voltage_l2_l3` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_iem3265_0020343063/ai13_voltage_l3_l1` | volt | not set | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| `bms_iem3265_0020343063/ai22_active_power_total` | kilowatt | not set | 0 - 10000 kW |
| `bms_iem3265_0020343063/ai23_reactive_power_total` | kilovolt ampere reactive | not set | 0 - 10000 kvar |
| `bms_iem3265_0020343063/ai24_apparent_power_total` | kilovolt ampere | not set | 0 - 10000 kVA |
| `bms_iem3265_0020343063/ai25_power_factor_total` | null | not set | set a real range |
| `bms_iem3265_0020343063/ai26_frequency` | hertz | not set | 45 - 65 Hz |
| `bms_iem3265_0020343063/ai44_meter_operation_time` | second | not set | 0 - open |

### 1d. Unit not set (`obix:units/null`) - 7 points

Without a unit we can neither sanity-check the unit nor apply a per-unit range.

| Point | Expected unit |
|---|---|
| `bms_enyecontrols/2f_power_m_reading_ai23` | kilowatt hour (energy) - confirm |
| `bms_enyecontrols/2f_water_m_reading_ai20` | cubic meter |
| `bms_enyecontrols/3f_power_m_reading_output_ai24` | kilowatt hour (energy) - confirm |
| `bms_enyecontrols/3f_water_m_reading_ai21` | cubic meter |
| `bms_enyecontrols/enyecontrolsbms_io_power_factor` | power factor (dimensionless or %) |
| `bms_enyecontrols/pm_power_factor_total_ai3` | power factor (dimensionless or %) |
| `bms_iem3265_0020343063/ai25_power_factor_total` | power factor (dimensionless or %) |

### 1e. Scale / semantics to confirm

- **BTU points** report unit `kilobtus per hour` but live values are ~1e6-7e6. That looks
  mis-scaled for the stated unit - please confirm the real unit/scale.
- `bms_enyecontrols/total_m3_ai32` is labelled `cubic meters per hour` (a flow rate) but the
  name suggests a running total volume. Unit may be wrong.
- `bms_enyecontrols/gf_power_m_reading_ai22` reports `kilowatt hour` (energy); if it is an
  instantaneous power reading it should be `kilowatt`.

### 1f. Suggested range by unit (for any point you are filling in)

| Unit | Suggested range |
|---|---|
| ampere | 0 - <meter rating> A |
| volt | 0 - 500 V (L-L) / 0 - 300 V (L-N) |
| hertz | 45 - 65 Hz |
| celsius | -40 - 60 C |
| percent | 0 - 100 % |
| parts per million | per gas: CO2 0-2000, CO 0-250 |
| micrograms per cubic meter | 0 - 500 ug/m3 |
| kilowatt | 0 - 10000 kW |
| kilowatt hour | 0 - open (cumulative total) |
| kilovolt ampere | 0 - 10000 kVA |
| kilovolt ampere reactive | 0 - 10000 kvar |
| cubic meter | 0 - open (cumulative total) |
| cubic meters per hour | 0 - 100 m3/h |
| kilobtus per hour | CONFIRM SCALE (values seen ~1e6-7e6) |
| second | 0 - open |

---

## 2. The 27 non-numeric points - confirm type and states

These points have **no numeric facets** - they are almost certainly boolean or enum
(occupancy, water level, sensor fault, device status). We cannot range-check them; we can
only check that the value is in an allowed set. Please confirm, for each, whether it is a
**boolean** or a **multi-state enum**, and list the allowed states.

- `bms_enyecontrols/3f_occu_hallway_bi19`
- `bms_enyecontrols/3f_occu_office_value_bi20`
- `bms_enyecontrols/co_sensor_fault_bi24`
- `bms_enyecontrols/enyecontrols_status`
- `bms_enyecontrols/enyecontrolsbms_io_2f_occu_dining_area`
- `bms_enyecontrols/enyecontrolsbms_io_2f_occu_fpl_manager`
- `bms_enyecontrols/enyecontrolsbms_io_2f_occu_hallway`
- `bms_enyecontrols/enyecontrolsbms_io_2f_occu_stairs`
- `bms_enyecontrols/enyecontrolsbms_io_3f_occu_hallway`
- `bms_enyecontrols/enyecontrolsbms_io_3f_occu_office`
- `bms_enyecontrols/enyecontrolsbms_io_a_gf_occu_stairs`
- `bms_enyecontrols/enyecontrolsbms_io_a_mz_occu_stairs`
- `bms_enyecontrols/enyecontrolsbms_io_a_occu_conference`
- `bms_enyecontrols/enyecontrolsbms_io_a_occu_engg_hallway`
- `bms_enyecontrols/enyecontrolsbms_io_a_occu_engg_manager`
- `bms_enyecontrols/enyecontrolsbms_io_a_occu_recieved_area`
- `bms_enyecontrols/enyecontrolsbms_io_a_water_level`
- `bms_enyecontrols/enyecontrolsbms_io_bm_water_level`
- `bms_enyecontrols/enyecontrolsbms_io_gf_occu_engg_hallway`
- `bms_enyecontrols/enyecontrolsbms_io_gf_occu_engg_manager`
- `bms_enyecontrols/enyecontrolsbms_io_gf_occu_stairs`
- `bms_enyecontrols/enyecontrolsbms_io_mezz_occu_conference`
- `bms_enyecontrols/enyecontrolsbms_io_mezz_occu_recieved_area`
- `bms_enyecontrols/enyecontrolsbms_io_mz_occu_stairs`
- `bms_enyecontrols/rh_1_sensor_fault_bi22`
- `bms_enyecontrols/temp_1_sensor_fault_bi23`
- `bms_iem3265_0020343063/iem3265_0020343063_systemstatus`

---

## 3. Which point set is authoritative?

There appear to be **two parallel sets** of the same measurements:

- `EnyecontrolsBMS_IO/*` - carries real engineering ranges (e.g. CO2 0-2000 ppm,
  Temp -40-60 C).
- `B: *_AIxx` (e.g. `B_3F_Temp Reading_AI37`) - carries only the generic 0-100 range,
  and some read 0.

Please confirm which set is live/authoritative so we read only that one (this also halves
the read load).

---

## 4. Enable write-back (optional, later)

Today the gateway is **read-only** - it publishes findings to BACnet and to its own API.
For the BMS to show a finding *against a plant object* (and later to accept an
acknowledge/control), we need a few **writable** points. If you are happy to add them,
please create them under:

```
/config/Drivers/ObixNetwork/exports
```

Minimum useful set (names are examples, any consistent scheme is fine):

| Purpose | Type | Notes |
|---|---|---|
| Gateway alive / heartbeat | boolean (writable) | true while the gateway is publishing |
| Finding status for a chosen meter | enum / string | e.g. OK / WARN / CRITICAL |
| Acknowledge for a finding | boolean (writable) | operator clears an active finding |

We will publish to exactly the points you nominate and nowhere else.

---

## 5. The 12 water meters - official register map

Our Modbus register map is still a **[DRAFT placeholder]**. We need the final map from the
meter datasheets / BMS point list. We currently assume 9 signals per meter:

`totalizer, pressure, reverse, pulse1, pulse2, battery, signal, online, alarm`

| Meter id | Modbus unit id | Signals we assume |
|---|---|---|
| `026026060001` | 1 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060002` | 2 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060003` | 3 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060004` | 4 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060005` | 5 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060006` | 6 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060007` | 7 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060008` | 8 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `026026060009` | 9 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `057A26060001` | 10 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `057A26060002` | 11 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |
| `057A26060003` | 12 (auto) | alarm, battery, online, pressure, pulse1, pulse2, reverse, signal, totalizer |

**Please provide:** the register addresses (holding/input), data type + byte/word order,
and scale factor per signal, for each meter.

---

## 6. Summary checklist

1. [ ] Set a correct **engineering range** for the points in 1a-1c (esp. Voltage L1-L2).
2. [ ] Set the **missing units** in 1d; confirm the **BTU scale** and the other suspected
   units in 1e.
3. [ ] Confirm the **type + allowed states** for the 27 points in section 2.
4. [ ] Confirm which of the **two point sets** is authoritative (section 3).
5. [ ] (Optional) create the **writable points** in section 4.
6. [ ] Provide the final **register map** for the 12 meters (section 5).

Nothing in sections 1-3 needs a code change on our side - when the ranges are set on the
station, our validator picks them up automatically on the next probe.
