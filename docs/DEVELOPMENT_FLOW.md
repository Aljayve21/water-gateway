# Enye BMS + AI — Development Flow

Single source of truth for **where we are** and **what comes next**. When in
doubt, this file wins. Update the status column as work lands.

---

## 1. North star

> Read the site's data on-prem, have AI **validate** it against the Neoview
> configuration, **explain** what's wrong, **recommend** action, and only
> **later** (and gated) **control** devices — then mirror it to AWS Bedrock.

The order is deliberate and non-negotiable: **read → validate → recommend →
control → cloud**. Each step earns the trust the next one needs.

---

## 2. How we work (rules)

1. **No AWS resources without explicit written permission.** The AWS plan stays
   a document until the user says go.
2. **Read-only by default.** Any write path (oBIX write, CMMS, device control)
   requires: an allow-list of writable points, validated value bounds, human
   approval, and an audit log. No exceptions.
3. **One capability at a time**, each with a *Definition of Done* gate (below).
4. **Every capability ships with tests** (`pytest`) and a doc/config update.
5. **Data separation**: `gateway_buffer` (water meters) and `obix` (Neoview)
   stay in separate databases. No cross-writes.
6. **Deterministic checks stay deterministic.** Rules are plain comparisons the
   operator owns; the AI *explains and recommends* on top, it never becomes the
   alerting engine (auditability).
7. **Never print credentials.** Tokens/keys live in `.env` (gitignored) or, on
   AWS, Secrets Manager.

---

## 3. The flow

```
  ✅ wired        ✅ wired          ✅ wired           ← current
┌──────────┐  ┌──────────┐  ┌────────────┐  ┌───────────────┐  ┌──────────────┐
│  READ    │→ │  STORE   │→ │  EXPOSE    │→ │  AI ACCESS     │→ │  VALIDATE    │
│ meters + │  │ buffer + │  │ /bms/*     │  │ /ai/ask tools  │  │ vs Neoview   │ ← WE ARE HERE
│ oBIX     │  │ obix DB  │  │ read-only  │  │ read buffer    │  │ config       │
└──────────┘  └──────────┘  └────────────┘  └───────────────┘  └──────┬───────┘
                                                                       │
      ┌──────────────┐  ┌───────────────┐  ┌────────────────────────────▼───────┐
      │  AWS MIRROR  │← │  CONTROL      │← │  RECOMMEND                          │
      │  (gated on   │  │  (gated:      │  │  AI proposes action; human decides  │
      │  permission) │  │  approval +   │  │  (on-demand + 1 daily summary)      │
      └──────────────┘  │  allow-list)  │  └─────────────────────────────────────┘
                        └───────────────┘
```

---

## 4. Phase tracker

| # | Phase | Goal | Status | Gate (Definition of Done) |
|---|---|---|---|---|
| 0 | Foundations | Config, DBs, single-instance, tests | ✅ done | suite green, gateway runs |
| 1 | Read — water meters | Modbus/BACnet extract → buffer | ✅ done | 12 meters live |
| 2 | Read — Neoview oBIX | Poll oBIX, store to `obix` DB | ✅ done | 140 points stored live |
| 3 | Expose — read-only API | `/bms/*` (points/devices/alarms/history/status) | ✅ done | bearer auth, scoping, paginated, smoke OK |
| 4 | AI access | `list_bms_points`, `get_bms_history` in `/ai/ask` | ✅ done | verified live end-to-end |
| 3b | Contract & docs | OpenAPI + AWS architecture doc | ✅ done | `docs/bms-openapi.yaml`, `docs/AWS_BMS_AGENT_ARCHITECTURE.md` |
| **6** | **Validate vs Neoview config** | **Flag data that doesn't fit point config (units, ranges, stale, frozen, enum) — for **oBIX/BMS** points** | 🟡 core done | validator + script + tests ✅; in `/bms/alarms` + `list_bms_findings` ✅; facet probe ready ✅ (run on-site to confirm) |
| 7 | Recommend | AI turns findings into ranked, explained recommendations | ⏳ planned | recommendation in answer + daily summary |
| 8 | Control | AI proposes writes → approved → oBIX write | 🔒 gated | writable points + approval flow + audit |
| 9 | AWS mirror | Bedrock Agent + API GW + Lambda (+KB) | 🔒 gated | permission + networking decision |

> The AWS build has its own finer schedule in
> [`docs/AWS_BMS_AGENT_ARCHITECTURE.md`](AWS_BMS_AGENT_ARCHITECTURE.md) §7.
> Phase 9 here = Phases 1–3 there.
>
> **Read API metadata:** `/bms/points` and its per-device/per-point variants now
> include a `requirements` block (expected `unit`, `min`, `max`, `tolerance`,
> `allowed`, `max_age_seconds`) resolved the *same way the validator resolves it*
> (`unit_bounds` → live Neoview facets → explicit override). A caller can see the
> band a value is judged against, without a second endpoint. Contract in
> [`docs/bms-openapi.yaml`](bms-openapi.yaml) (`Requirements`).
>
> **Equipment naming:** point reads also carry `equipment_id`/`equipment_point`
> — the names the plant uses (`AHU-01`/`SpaceTemp`) rather than raw oBIX ids.
> Sourced from `config/equipment_map.yaml`, falling back to the station's own
> device/point names for anything unmapped, so it works before the map is
> authored. `GET /bms/equipment` lists every point under both names and
> `GET /bms/points/{equipment_id}/{equipment_point}` accepts an equipment
> address. See [`src/gateway/equipment.py`](../src/gateway/equipment.py).

---

## 5. Current phase — Validate vs Neoview config

**Goal:** for every oBIX point, decide whether the *live value* is consistent
with the point's *configured requirements* (unit, bounds, allowed states,
freshness), and emit findings.

**Config source (the honest bit):** the ideal source is Neoview's own point
facets (min/max/precision). We have **not yet confirmed** Neoview exposes them
over oBIX (the station was unreachable when we last tried), so we built a
read-only probe (`scripts/probe_facets.py`) that reads them and caches the
result when the station *is* reachable. Requirements resolve in four layers
(later wins): `defaults` → `unit_bounds` (by unit) → live Neoview `facets` →
explicit `points` override. When there are no facets the authored file
(`config/obix_requirements.yaml`) is the source of truth.

**Rules (deterministic, like `ai/anomaly.py`):**

| Rule | Trigger |
|---|---|
| `no_value` | point has no latest value at all |
| `unit_mismatch` | reported unit ≠ required unit |
| `out_of_range` | numeric value outside `[min, max]` |
| `enum_invalid` | text value not in the allowed set |
| `stale` | `captured_utc` older than `max_age_seconds` |
| `frozen` *(opt-in)* | identical value across N hourly buckets |

**Findings** are surfaced via `scripts/validate_obix.py` now; later they join
`/bms/alarms` and the AI tools.

### Definition of done — this phase
- [x] `config/obix_requirements.yaml` exists with units + bounds.
- [x] `src/gateway/obix_validation.py` implements the rules above.
- [x] `scripts/validate_obix.py` runs against the `obix` DB.
- [x] `tests/test_obix_validation.py` covers each rule (14 tests).
- [x] graceful Neoview facet probe (`scripts/probe_facets.py`) + cache-backed
  fallback (facets → cache → authored file), auto-refreshed after a good read.
- [x] confirm Neoview actually exposes facets — **yes**: `scripts/probe_facets.py`
  read facets for **114 of 141** points; the other 27 are boolean/occupancy/status
  points that have no numeric facets (0 probe errors).
- [x] surface findings in `/bms/alarms` and as an AI tool (`list_bms_findings`).

### What landed (first slice)
- `src/gateway/obix_validation.py` — `ObixRequirements`, `Requirement`,
  `Finding`, `ObixValidator` (rules: `no_value`, `unit_mismatch`,
  `out_of_range`, `enum_invalid`, `stale`, `frozen`).
- `config/obix_requirements.yaml` — 4-layer resolution (`defaults` →
  `unit_bounds` by unit → live Neoview `facets` → `points` override).
- `scripts/validate_obix.py` — read-only CLI, `--json`, `--meter`,
  `--fail-on {info,warning,critical,never}`.
- Live result on 140 points: 3 critical `out_of_range` (3F temp = 0,
  two frequency points = 0). `stale` correctly silent (data was 13 min old).

### Findings surfaced (second slice)
- `GET /bms/alarms` now merges two sources into one severity-sorted list:
  water-meter anomalies, plus BMS requirement findings tagged
  `source: obix_validation`.
- New AI tool `list_bms_findings` (`min_severity`, `meter_id`, `limit`), so
  `/ai/ask` can answer *"which BMS points look wrong?"* — advertised only when
  the oBIX store is present, scoped per principal like the other BMS tools.
- One `ObixValidator` is built in `main()` (not inside the AI stack) and shared
  by the tools, the HTTP server, and the oBIX loop, so a live facet refresh
  applies to all of them at once.

### Neoview facet probe (third slice)
- `src/gateway/obix.py` — `Facets` + `parse_facets()`, and
  `ObixClient.get_facets()` / `read_facets()`. Reads Niagara's own
  `min`/`max`/`precision`/`unit` facets, trying the point object then its
  `/value` child; an object with no facets is not an error.
- `src/gateway/obix_facets.py` — `probe_facets()` walks the read map and records
  per-point errors. It aborts after a few consecutive transport errors **only
  until the first facet is read**, so an offline station does not pay 140
  timeouts while a reachable station with a few bad points keeps going.
  `save/load_facets_cache` persist a JSON cache under `config/obix_facets.json`
  (gitignored).
- `scripts/probe_facets.py` — read-only CLI (`--meter`, `--json`, `--no-cache`,
  `--require-facets`). Writes the cache and exits 1 when the station is
  unreachable. Run this on-site to answer *"does Neoview expose facets?"*.
- Validation picks the cache up automatically: facet bounds override
  `unit_bounds`, facet `precision` becomes an `out_of_range` tolerance, and an
  explicit `points` entry still wins. The gateway re-probes every
  `OBIX_FACETS_REFRESH_HOURS` (default 24) **after a successful read** and swaps
  the shared validator's requirements in place.
- `tests/test_obix_facets.py` — 30 tests (parsing, `/value` fallback, probe
  abort, cache round-trip, layer precedence, precision tolerance, unit trust,
  degenerate-bound guard).

**Live result (station reachable).** `scripts/probe_facets.py` read facets for
**114/141** points; the remaining 27 are boolean/occupancy/status points with no
numeric facets. `config/obix_facets.json` now holds real station ranges, so
validation runs on Neoview's own config rather than only the authored file. Two
things the probe immediately exposed, both worth a controls-team conversation:

- `EnyecontrolsBMS_IO/Voltage L1-L2` reports an engineering range of **0–100** but
  reads **242.8 V** — the station range is wrong (a 3-phase L-L point), so the
  facet layer flags it as `out_of_range` (true finding: bad station metadata).
- Several points report **min = max = 0** (range never set). Degenerate bounds
  are now ignored rather than treated as a zero-width range.

---

## 6. Dependency tracker (what blocks what)

| Need | Owner | Unblocks |
|---|---|---|
| Neoview point facets availability | **done** — facets readable (114/141); station ranges are now the live config source | ~~better config source~~ |
| Correct Neoview engineering ranges (L1-L2 volt 0–100 but reads 242.8; unset 0/0 ranges) | controls team | fewer false positives |
| Point requirements (units/limits) | controls team / us | this phase, quality |
| Writable oBIX points (`/config/Drivers/ObixNetwork/exports`) | controls team | Phase 8 (control) |
| 12-meter register map | controls team | water-meter accuracy |
| CMMS choice + API contract | user | `POST /cmms/workorder` |
| How AWS reaches on-prem (A/B/C/D) | user + network | Phase 9 |

---

## 7. Conventions

- **DBs:** `gateway_buffer`/`gateway_test` (meters), `obix`/`obix_test` (Neoview).
- **User:** `gateway_buf` (no DDL beyond its own DBs); `root` only for provisioning.
- **Runs/tests:** `.venv\Scripts\python.exe`; `pytest -q` from `water-gateway`.
- **Config:** YAML under `config/`; secrets in `.env`.
- **Naming:** point ids are the oBIX point names (e.g. `b_2f_co2_reading_ai34`);
  pseudo-meter ids are `bms_<device>` (e.g. `bms_enyecontrols`).
