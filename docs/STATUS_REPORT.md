# Status Report

Generated: 2026-10-19 23:44:00 UTC (updated after each feature)
Repo: water-gateway (branch: main)
Latest commit: e84fc79

## Completed

### Read / API (GET)
- [x] `/bms/points`, `/bms/points/{meter}`, `/bms/points/{meter}/{point}` — read-only, bearer auth, scoping
- [x] `/bms/devices`, `/bms/status` (operator-only), `/bms/history`, `/bms/alarms` (meter anomalies + oBIX findings)
- [x] Expose `requirements` (unit/min/max/tolerance/allowed/max_age_seconds) on point reads, resolved same as validator
- [x] Expose `writable: boolean` on point reads (defaults to false; consults write map when present)

### oBIX / BMS
- [x] Read 141 Neoview oBIX points → `obix` DB; hourly history
- [x] Neoview facet probe (114/141 points) + cache → `config/obix_facets.json` (gitignored)
- [x] Validator (no_value, unit_mismatch, out_of_range, enum_invalid, stale, frozen opt-in); findings surface in `/bms/alarms` + `list_bms_findings` tool
- [x] 4-layer requirement resolution: defaults → unit_bounds → live Neoview facets → explicit points override; degenerate bounds (min==max) ignored
- [x] Shared validator across AI tools/HTTP loop (facet refresh applies to all)

### Water meters
- [x] 12 meters read (Modbus/BACnet paths) → `gateway_buffer` DB

### AI integration
- [x] Tools: `list_bms_points`, `get_bms_history`, `list_bms_findings` (with store present; scoped)

### Docs / Config / Testing
- [x] `docs/DEVELOPMENT_FLOW.md`, `docs/bms-openapi.yaml`, `docs/AWS_BMS_AGENT_ARCHITECTURE.md`, `docs/CONTROLS_TEAM_REQUEST.md`, `docs/INNOTECH_SITE_VISIT.md`, `docs/STATUS_REPORT.md`
- [x] `tests/test_obix_facets.py` (30), `tests/test_obix_validation.py` (14), full suite 371 passed

## Pending (GET-only focus)

### Controls / Data correctness (blocked on site)
- [ ] Correct Neoview engineering ranges (esp. Voltage L1-L2 0–100 vs ~244 V; unset/min==max=0 ranges) — see `docs/CONTROLS_TEAM_REQUEST.md`
- [ ] Confirm 27 non-numeric points (type + allowed states)
- [ ] Confirm which point set is authoritative (`EnyecontrolsBMS_IO/*` vs `B:*_AIxx`)

### API polish (GET)
- [ ] Decide `/bms/alarms` source: keep derived findings only, or also merge Neoview's native alarms (separate read)

### Mapping / Naming
- [ ] Equipment ↔ point naming map (e.g. `AHU-01/SpaceTemp` ↔ `bms_enyecontrols/*`) — needed if consumers think in equipment

### Infra / Ops (GET-side)
- [ ] Confirm running background gateway lifecycle (keep up / restart policy)
- [ ] Credential hygiene (cPanel token, AWS keys, `AI_BEARER_TOKEN`) rotation/check
- [ ] Review untracked root files (commit as reference or delete)

### Explicitly gated (do NOT start)
- [ ] POST / write paths (oBIX writes, control) — allow-list + approval + audit required
- [ ] CMMS: `POST /cmms/workorder` — no contract yet
- [ ] AWS mirror (API GW/Lambda/Bedrock KB/RDS/DynamoDB) — needs explicit permission + networking decision

## Notes
- All changes since last report: added `writable: boolean` to `/bms/*` point reads (commit e84fc79). Tests: 371/371 passed.
- Focus is GET-only; POST/CMMS/AWS remain frozen.
