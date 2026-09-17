# Python gateway for the Enye EMS / Water Meter system.

Read-only integration/middleware that reads data from the office MySQL `mdpf`
database and exposes normalized water-meter data over **Modbus TCP** and
**BACnet/IP** to BMS/EMS consumers.

> **Office MySQL `mdpf` is the source of truth. This gateway is READ-ONLY
> toward the office database — it never executes INSERT / UPDATE / DELETE /
> ALTER / DROP, never changes users, permissions, or schema.**

It does **not** replace the existing office receiver, does not modify meter/logger
configuration, and does not touch any existing production service.

## Status

- **Phase 1 (current):** project skeleton + configuration layer (env + YAML),
  structured logging, graceful-startup main loop. No MySQL connection yet.
- Pipeline (extractor → buffer → normalizer → mapping), Modbus store/server and
  BACnet store/server are implemented in later phases.

## Structure

```
water-gateway/
├── config/               # YAML mapping templates + operational policy (no secrets)
│   ├── gateway.yaml      # poll cadence, thresholds, buffer retention, logging
│   ├── base_points.yaml  # canonical point vocabulary (source column, unit)
│   └── meters.yaml       # per-meter unit_id + point -> Modbus/BACnet maps [DRAFT]
├── src/gateway/          # application package
│   ├── config.py         # .env (Settings) + YAML loaders (Pydantic-validated)
│   ├── logging_setup.py  # structured JSON logging (console + rotating file)
│   └── main.py           # bootstrap + graceful shutdown loop
├── scripts/
│   └── introspect_schema.py  # READ-ONLY schema dump against office mdpf
├── tests/                # pytest suite
└── .env.example          # placeholders only — never commit real .env
```

## Quickstart (development)

```powershell
# 1. Create & activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Local development config (never commit the real one)
Copy-Item .env.example .env
# edit .env with the read-only MySQL account + bind addresses

# 4. Run
python -m gateway.main        # from src\
```

## Tests

```powershell
pytest
```

## Read-only contract

- Requires a MySQL account with **SELECT only** on the office `mdpf` tables the
  gateway needs. The gateway never creates or alters that account.
- `scripts/introspect_schema.py` only runs informational `SELECT`s against
  `information_schema` (and `SHOW TABLES`-equivalent reads). It never writes.

## Not final (owner-pending)

Modbus registers/coils and BACnet object addresses in `config/meters.yaml` are
marked **[DRAFT]** until the official BMS point list is supplied."# water-gateway" 
