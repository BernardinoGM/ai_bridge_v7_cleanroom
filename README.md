# AI Bridge clean-room rebuild v7

This is a standalone clean-room rebuild of AI Bridge for launch on Railway. It does not inherit public cost-plus pricing, public take-rate logic, cashback loops, commission trees, or consumer VPN assumptions.

## Product model

- `Fast`: cheapest default lane for lightweight and medium requests.
- `Smart`: main product lane with intelligent routing and limited premium escalation.
- `Assured`: premium lane for high-risk and release-check workflows.

Public pricing is pack and product-layer based. Benchmark and gross-margin guardrails exist only as internal telemetry.

## Stack

- FastAPI
- SQLAlchemy
- Jinja templates for landing and dashboard pages
- Stripe Checkout + verified webhook crediting
- SQLite for local development, Postgres-ready via `DATABASE_URL`
- Lightweight agent profile and task continuity layer for stable `/v1/messages` work

## Launch features

- Landing page aligned to the new positioning
- Runway-centric dashboard
- Top-up packs: Starter, Growth, Scale
- Stripe checkout session creation
- Verified webhook with idempotent wallet crediting
- Closed-loop referral perks on first paid top-up only
- Add-on architecture for Bill Guard, Team Vault, Priority Queue, Custom Routing Rules, and Analytics Pro
- `/api/chat/completions` and `/api/messages` with launch-safe non-streaming billing
- `/v1/messages` task continuity with pinned lanes, pinned execution profiles, explicit escalation/de-escalation, and timeout-based reset
- Internal telemetry for route choice, premium escalation, serving COGS, benchmark estimate, QA trigger rate, and latency
- User-facing surfaces expose `Fast`, `Smart`, `Assured`, `In progress`, `Checked`, and `Verified` instead of backend model names
- Production execution uses remote commercial-grade provider adapters only; mock providers are only enabled explicitly for testing or development

## Quick start

```bash
cd /Users/forrest/ai_bridge_v7_cleanroom
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

Open:

- Landing page: `http://127.0.0.1:8000/`
- Demo dashboard: `http://127.0.0.1:8000/dashboard/demo`
- Health: `http://127.0.0.1:8000/api/health`

## Stripe flow

1. Client requests `POST /api/payments/checkout`.
2. Server creates Stripe Checkout Session and records a pending `payment_records` row.
3. Stripe sends `checkout.session.completed` webhook.
4. Webhook signature is verified.
5. App credits main balance exactly once using the processed webhook table and unique wallet ledger refs.
6. If eligible, a one-time perk is credited to the referrer in the non-withdrawable promo bucket.

There is no public self-credit endpoint.

## Task continuity

`/v1/messages` is treated as a stable work surface rather than a fresh routing decision every turn.

- Each user gets exactly one lightweight `agent_profile`.
- Each ongoing task gets a `task_session` with pinned lane state and pinned execution profile.
- Follow-up turns keep the same execution path unless there is explicit escalation, de-escalation, timeout, or failure recovery.
- Standard users do not see backend provider labels or route traces.

## User-agent binding

The launch agent layer is a logical per-user decision profile, not a long-running worker:

- one `AgentProfile` per user
- lazily created on first request
- DS-first default provider family for normal work
- lightweight counters and hints instead of raw chat-history storage
- task continuity takes priority over agent preference, then requested mode, then system default

The agent updates after each turn with compact signals such as premium escalation, fallback, QA trigger, stable-task reuse, DS-path success, and rolling 7-day health metrics.
Those signals are then reused to keep normal work on the cheaper stable DS path, suppress unnecessary repeated QA for stable users/tasks, and avoid premium escalation unless the risk pattern justifies it.

## Launch provider posture

The launch version is remote-only:

- Railway hosts the app layer only.
- Fast, Smart, and Assured all run through remote commercial-grade providers.
- Production traffic never depends on the founder laptop or any consumer machine.
- Consumer VPN is not part of the production path.
- Local or self-hosted inference remains future-ready in the provider abstraction but is disabled by default.

## Auto-reload

Launch includes schema and config surface for threshold-based auto-reload. It is feature-flagged by default because production activation should use saved payment methods and an explicit Stripe-safe flow.

## Local inference future

Provider settings support remote Fast, remote Smart, and remote Assured launch lanes. Future local or self-hosted inference remains disabled by default. Railway should run the clean-room app with `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`. See [docs/ARCHITECTURE_NOTES.md](/Users/forrest/ai_bridge_v7_cleanroom/docs/ARCHITECTURE_NOTES.md).

## Smoke tests

See [docs/SMOKE_TESTS.md](/Users/forrest/ai_bridge_v7_cleanroom/docs/SMOKE_TESTS.md).
