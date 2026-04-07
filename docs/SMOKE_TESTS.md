# Smoke test commands

## Local boot

```bash
cd /Users/forrest/ai_bridge_v7_cleanroom
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

## Endpoints

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/ | head
curl -s http://127.0.0.1:8000/api/topups/packs
curl -s http://127.0.0.1:8000/dashboard/demo | head
```

## Chat compatibility

```bash
curl -s -X POST http://127.0.0.1:8000/api/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "mode": "smart",
    "messages": [{"role":"user","content":"Summarize the launch value proposition."}]
  }'
```

## Stable task continuity

```bash
curl -s -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "mode": "smart",
    "messages": [{"role":"user","content":"Prepare a release-check plan for launch."}]
  }'
```

Take the returned `task_id`, then continue the same task:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "mode": "fast",
    "task_id": "REPLACE_TASK_ID",
    "messages": [{"role":"user","content":"Continue the same task with tighter acceptance checks."}]
  }'
```

The response should keep the same visible lane for the active task and should not expose backend route labels.
For a premium-like task, the continuation should also stay on the same execution profile unless you explicitly escalate, de-escalate, or let the task expire.

## User-bound agent profile

Create two tasks for the same user:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "mode": "smart",
    "messages": [{"role":"user","content":"Summarize the weekly operations note."}]
  }'
```

```bash
curl -s -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "mode": "fast",
    "messages": [{"role":"user","content":"Draft a short recap email for the same team."}]
  }'
```

Then inspect the bound agent profile:

```bash
curl -s http://127.0.0.1:8000/api/admin/agents/1 \
  -H "X-Admin-Key: admin-dev-key"
```

You should see one logical agent profile for the user, with DS-first defaults and lightweight counters such as premium trigger count and DS success rate.

## Admin-only telemetry

```bash
curl -s http://127.0.0.1:8000/api/admin/usage/REPLACE_REQUEST_ID \
  -H "X-Admin-Key: admin-dev-key"
```

## Tests

```bash
pytest
```
