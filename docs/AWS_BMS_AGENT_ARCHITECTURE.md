# AWS BMS Agent — Architecture & Rollout Plan

Status: **design / not started**. No AWS resources have been created. This
document describes the intended target architecture for connecting the on-prem
water-gateway to an Amazon Bedrock Agent, and the decisions that gate each step.

---

## 1. Goal

Let a conversational agent answer questions about the site's water meters and
the Neoview/BMS building system, and (later) raise CMMS work orders — using the
on-prem gateway as the single source of truth.

Two distinct data sets are involved:

| Data set | Source | Gateway store | Endpoint family |
|---|---|---|---|
| Water meters (12) | Modbus/BACnet extractor | `gateway_buffer` (normalized + history) | `/ai/*` tools |
| Neoview/BMS (141 oBIX points) | oBIX poll (60s) | separate `obix` DB (+ buffer mirror) | `/bms/*` |

---

## 2. What already exists (on-prem, built)

```
Neoview station ──oBIX(80/443)──▶ water-gateway ──▶ MySQL
  (EnyeBMS1)                      • poll loop (60s)      ├─ gateway_buffer
                                  • anomaly findings     └─ obix DB
                                  • Bedrock assistant (:9091 /ai/*)
                                  • read-only BMS API (:9091 /bms/*)
```

* `/bms/points`, `/bms/points/{meter_id}`, `/bms/points/{meter_id}/{point_id}`,
  `/bms/devices`, `/bms/status`, `/bms/alarms`, `/bms/history` — bearer-auth,
  principal-scoped, paginated. Contract: [`docs/bms-openapi.yaml`](bms-openapi.yaml).
* Local Bedrock assistant (`/ai/ask`) with read-only tools; optional oBIX tools
  (`list_bms_points`, `get_bms_history`, `list_bms_findings`) when the oBIX
  store is wired in. `list_bms_findings` reports points that violate
  `config/obix_requirements.yaml`; the same findings feed `GET /bms/alarms`.
* Once-daily automatic summary (`AI_SUMMARY_LOCAL_TIME`).

---

## 3. Target architecture (AWS)

```
                       ┌───────────────────────────── AWS ─────────────────────────────┐
                       │                                                                │
 User / Teams ──HTTPS──▶ API Gateway ──▶ Lambda (integration) ──▶ Bedrock Agent + KB    │
                       │  (auth, throttle,   (validate, transform,    ▲   (S3 docs/SOPs)│
                       │   routing, logs)     call on-prem /bms,      │                 │
                       │                      CMMS, logging)          │ action groups   │
                       │                            │                 │                 │
                       │                            ├──▶ Secrets Manager (tokens)       │
                       │                            ├──▶ DynamoDB/RDS (cache, optional)  │
                       │                            └──▶ CloudWatch (logs, metrics)      │
                       └───────────────────────────────┬────────────────────────────────┘
                                                       │  ← the hard part: how?
                                                       ▼
                              on-prem water-gateway  /bms/*   (must be reachable)
```

### 3.1 Networking — the gating decision

Lambda cannot reach a private on-prem HTTP endpoint. Choose one:

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Public reverse proxy** | Expose `/bms/*` on `https://ems.enye.com.ph` behind the office router | Simplest; reuses existing Apache vhost | Needs Omada NAT/DMZ + firewall rule (currently blocked); on-prem API becomes internet-facing |
| **B. Site-to-site VPN / Direct Connect** | AWS VPC ⇄ office router | Private, robust | Router must support IPsec; ongoing ops |
| **C. Invert the flow (push)** | Gateway pushes readings/findings to AWS (API Gateway / IoT Core / SQS); AWS never dials in | No inbound NAT; works today | AWS side becomes the store; agent reads AWS cache, not live on-prem |
| **D. Hybrid** | Push to AWS for the agent; keep `/bms/*` for local/ops | Best UX, no inbound | Most moving parts |

Recommendation: **C or D** if the NAT change keeps slipping; **A** if a public
HTTPS endpoint is acceptable and the Omada change can be made.

---

## 4. Components

### 4.1 Amazon API Gateway
* HTTP API, custom domain, TLS.
* Authorizer: JWT (Cognito) or IAM; API keys only for low-risk internal clients.
* Usage plans / throttling; WAF if public.
* CloudWatch access logging + X-Ray.

### 4.2 Lambda — integration layer
* Validates and transforms requests, calls on-prem `/bms/*`, maps errors.
* Holds the on-prem bearer token in **Secrets Manager** (never in env plaintext).
* Optional short-TTL cache (DynamoDB) for catalog/latest to cut on-prem calls.
* Logs structured events to CloudWatch; emits custom metrics (latency, errors).

### 4.3 Bedrock Agent + Knowledge Base
* Action group schema: import `docs/bms-openapi.yaml` (paths become agent tools).
* Model: Nova / Claude per the account's Bedrock access.
* S3 Knowledge Base: SOPs, manuals, escalation runbooks (non-time-series docs).
* Guardrails: no write actions until Track B is ready.

### 4.4 Optional Amazon RDS
Only if the agent needs to query history at scale or the push model (C) is
chosen. Otherwise the on-prem `obix` DB stays the system of record and RDS is
unnecessary.

---

## 5. Authentication model

```
Client ──JWT──▶ API Gateway ──invokes──▶ Lambda ──Bearer(token)──▶ on-prem /bms/*
                                              (Secrets Manager)
```

* End users authenticate to Cognito/API Gateway.
* Lambda authenticates to the on-prem gateway with a per-service bearer token
  from `AI_PRINCIPALS` (role ADMIN/TECHNICIAN, or a dedicated service subject).
* On-prem still enforces meter scoping; Lambda forwards a per-user subject only
  if end-user-level scoping is required (recommended once multi-tenant).

---

## 6. Request and proactive flows

**Interactive (exists conceptually):**
`user → API GW → Lambda → Bedrock Agent → {action group → Lambda → /bms/*} → answer`

**Proactive (new, desired "recommendations"):**
`EventBridge (schedule) → Lambda → read /bms/alarms + /bms/history → Bedrock Agent
 → recommendation → SNS/SES/Teams (or CMMS).`
The local assistant is request-driven today; only the daily summary is automatic.

---

## 7. Phased rollout

**Phase 0 — on-prem hardening (in progress, local)**
- [x] Read-only `/bms/*` with auth, scoping, pagination.
- [x] OpenAPI contract (`docs/bms-openapi.yaml`).
- [x] AI tools for oBIX catalog/history/findings.
- [ ] TLS on the on-prem API (if exposed).
- [ ] Dedicated service principal for the AWS integration.

**Phase 1 — reachability (decide A/B/C/D, then build one)**
- [ ] Networking decision + change window.
- [ ] Smoke test AWS→on-prem path.

**Phase 2 — AWS read path**
- [ ] Secrets Manager secret for the on-prem token.
- [ ] API Gateway + Lambda read integration + CloudWatch.
- [ ] Import action group schema into the Bedrock Agent.

**Phase 3 — knowledge & proactive**
- [ ] S3 Knowledge Base (SOPs/manuals).
- [ ] EventBridge scheduled recommendation job.

**Phase 4 — write path (gated, see §8)**
- [ ] CMMS `/cmms/workorder`.
- [ ] `PUT /bms/points/{id}` (needs writable oBIX publish points).

---

## 8. Prerequisites / blockers

| Item | Blocked on |
|---|---|
| Public HTTPS endpoint | Omada NAT/DMZ change (router login) |
| `PUT/POST /bms/points` | Writable points in Neoview `/config/Drivers/ObixNetwork/exports` (currently empty) — controls team |
| `POST /cmms/workorder` | CMMS API contract — not yet defined |
| Bedrock Agent actions | Phase 2 completion |

---

## 9. Explicit non-goals (for now)

* No AWS resources created as part of this document.
* No secrets committed; tokens live in `.env` (gitignored) or Secrets Manager.
* No autonomous write actions until Phase 4 prerequisites are met.
