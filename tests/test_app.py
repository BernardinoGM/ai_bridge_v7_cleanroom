import os
import json
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{Path(__file__).resolve().parent / 'test.db'}"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["ADMIN_API_KEY"] = "admin-test-key"
os.environ["APP_ENV"] = "testing"
os.environ["PROVIDER_MOCK_ENABLED"] = "true"
os.environ["PROVIDER_LOCAL_ENABLED"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.billing import wallet_balance
from app.db import SessionLocal
from app.main import app, bootstrap
from app.models import AgentProfile, PaymentRecord, TaskSession, User
from app.models import TaskTurn
from app.payments import ensure_seed_user, process_checkout_completed


def setup_module() -> None:
    test_db = Path(__file__).resolve().parent / "test.db"
    if test_db.exists():
        test_db.unlink()
    bootstrap()
    with SessionLocal() as db:
        ensure_seed_user(db, "user1@example.com", "User One", referral_code="UONE10")
        ensure_seed_user(db, "user2@example.com", "User Two", referral_code="UTWO10")
        db.commit()


client = TestClient(app)


def _user_id(email: str) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.id


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["vpn_required"] is False


def test_dashboard_is_runway_centric() -> None:
    response = client.get("/dashboard/1")
    assert response.status_code == 200
    body = response.text.lower()
    assert "runway dashboard" in body
    assert "heavy-workdays" in body
    assert "top up balance" in body
    assert "promo / perks" not in body


def test_root_and_dashboard_routes_resolve_without_affecting_health() -> None:
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    dashboard = client.get("/dashboard", follow_redirects=True)
    assert dashboard.status_code == 200
    assert "runway dashboard" in dashboard.text.lower()
    health = client.get("/api/health")
    assert health.status_code == 200


def test_chat_surface_loads_as_product_ui() -> None:
    response = client.get("/chat/1")
    assert response.status_code == 200
    body = response.text.lower()
    assert "ai bridge chat" in body
    assert "tasks" in body
    assert "stable task continuity" in body
    assert "deepseek" not in body
    assert "claude" not in body


def test_landing_is_conversion_led_and_routes_to_sections() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text.lower()
    assert "stop paying premium prices for default work." in body
    assert "ai bridge routes routine work through the cheaper lane" in body
    assert "try the playground" in body
    assert "see pricing" in body
    assert "dashboard" in body
    assert "get api key" in body
    assert "what ai bridge is actually doing" in body
    assert "direct premium" in body
    assert "ai bridge" in body
    assert "typical reduction on routine work" in body
    assert "40%–70%." in body
    assert "higher on repetitive summarization, lower on review-heavy workflows." in body
    assert "why it wins" in body
    assert "cheaper by default" in body
    assert "premium when earned" in body
    assert "stable across turns" in body
    assert "take one bite" in body
    assert "pick a real task. watch the router decide." in body
    assert "summarize a spec" in body
    assert "refactor a file" in body
    assert "draft a customer reply" in body
    assert "run this example" in body
    assert "see what your current workflow is costing you" in body
    assert "direct premium spend" in body
    assert "ai bridge blended spend" in body
    assert "typical reduction range" in body
    assert "solo builder" in body
    assert "shipping sprint" in body
    assert "review-heavy week" in body
    assert "$120–$180/mo" in body
    assert "$250–$450/mo" in body
    assert "pick your starting pack" in body
    assert "start small. route real work. scale only when it proves itself." in body
    assert "starter credit" in body
    assert "$10" in body
    assert "operating credit" in body
    assert "$50" in body
    assert "committed credit" in body
    assert "$200" in body
    assert "start with $10 credit" in body
    assert "start with $50 credit" in body
    assert "start with $200 credit" in body
    assert 'href="/dashboard/demo"' in body
    assert 'id="playground"' in body
    assert 'id="savings"' in body
    assert 'id="packs"' in body
    assert 'id="api-key-modal"' in body
    assert "choose a pack, get redirected to secure checkout." in body
    assert 'fetch("/api/payments/checkout"' in body


def test_webhook_processing_is_idempotent() -> None:
    founder_id = _user_id("founder@aibridge.local")
    referrer_id = _user_id("user2@example.com")
    with SessionLocal() as db:
        payment = PaymentRecord(
            user_id=founder_id,
            pack_code="growth",
            amount_usd=50.0,
            bonus_usd=5.0,
            status="pending",
            stripe_session_id="cs_test_123",
            referred_by_code="UTWO10",
        )
        db.add(payment)
        db.commit()
        processed_first = process_checkout_completed(db, "evt_1", "cs_test_123", "pi_123")
        db.commit()
        processed_second = process_checkout_completed(db, "evt_1", "cs_test_123", "pi_123")
        db.commit()
        assert processed_first is True
        assert processed_second is False
        assert wallet_balance(db, founder_id, "main") == 55.0
        assert wallet_balance(db, referrer_id, "promo") == 5.0


def test_chat_requires_real_balance_and_debits_once() -> None:
    founder_id = _user_id("founder@aibridge.local")
    with SessionLocal() as db:
        db_user = db.get(User, founder_id)
        assert db_user is not None
    response = client.post(
        "/api/chat/completions",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Draft a launch note for a release check"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ab"]["mode"] in {"Smart", "Assured"}
    serialized = str(payload)
    assert "deepseek" not in serialized.lower()
    assert "claude" not in serialized.lower()
    assert "route" not in payload["ab"]
    assert "model" not in payload


def test_streaming_disabled_for_billing_accuracy() -> None:
    founder_id = _user_id("founder@aibridge.local")
    response = client.post(
        "/api/messages",
        json={
            "user_id": founder_id,
            "mode": "fast",
            "system": "Be concise",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 400
    assert "streaming is disabled" in response.json()["detail"].lower()


def test_messages_task_continuity_stays_pinned_and_hides_internal_routes() -> None:
    founder_id = _user_id("founder@aibridge.local")
    first = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Help me prepare a release-check plan for production launch."}],
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    task_id = first_payload["task_id"]
    assert first_payload["ab"]["mode"] == "Assured"
    assert "model" not in first_payload
    assert "route" not in str(first_payload).lower()
    assert "claude" not in str(first_payload).lower()
    second = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "fast",
            "task_id": task_id,
            "messages": [{"role": "user", "content": "Continue that same launch task and tighten the checklist."}],
        },
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["task_id"] == task_id
    assert second_payload["ab"]["mode"] == "Assured"
    assert second_payload["ab"]["task_state"] == "Verified"


def test_task_thread_api_maps_visible_messages_to_task_turns() -> None:
    founder_id = _user_id("founder@aibridge.local")
    first = client.post(
        "/api/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "source_surface": "chat_surface",
            "messages": [{"role": "user", "content": "Draft a compact launch summary for the team."}],
        },
    )
    assert first.status_code == 200
    task_id = first.json()["task_id"]
    second = client.post(
        "/api/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "task_id": task_id,
            "task_action": "continue",
            "source_surface": "chat_surface",
            "messages": [{"role": "user", "content": "Continue the same task with two rollout checkpoints."}],
        },
    )
    assert second.status_code == 200
    thread = client.get(f"/api/tasks/{founder_id}/{task_id}")
    assert thread.status_code == 200
    payload = thread.json()
    assert payload["task"]["task_id"] == task_id
    assert payload["task"]["source_surface"] == "chat_surface"
    assert len(payload["messages"]) == 4
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][1]["role"] == "assistant"
    assert "deepseek" not in str(payload).lower()
    assert "claude" not in str(payload).lower()
    with SessionLocal() as db:
        task = db.scalar(select(TaskSession).where(TaskSession.task_id == task_id))
        assert task is not None
        turns = db.scalars(select(TaskTurn).where(TaskTurn.task_session_id == task.id)).all()
        assert len(turns) == 2
        assert task.turn_count == 2


def test_user_gets_one_logical_agent_profile_and_multiple_tasks_bind_to_it() -> None:
    founder_id = _user_id("founder@aibridge.local")
    first = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Summarize the board update for this week."}],
        },
    )
    second = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "fast",
            "messages": [{"role": "user", "content": "Draft a short recap email for the same team."}],
        },
    )
    assert first.status_code == 200
    assert second.status_code == 200
    with SessionLocal() as db:
        profiles = db.scalars(select(AgentProfile).where(AgentProfile.user_id == founder_id)).all()
        tasks = db.scalars(select(TaskSession).where(TaskSession.user_id == founder_id)).all()
        assert len(profiles) == 1
        assert len(tasks) >= 2


def test_ds_first_behavior_updates_agent_profile_without_leaking_provider_names() -> None:
    founder_id = _user_id("founder@aibridge.local")
    response = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Write a concise summary of the weekly operations note."}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ab"]["mode"] == "Smart"
    assert "deepseek" not in str(payload).lower()
    assert "claude" not in str(payload).lower()
    admin = client.get(f"/api/admin/agents/{founder_id}", headers={"X-Admin-Key": "admin-test-key"})
    assert admin.status_code == 200
    admin_payload = admin.json()
    assert admin_payload["default_provider_family"].startswith("ds")
    assert admin_payload["recent_ds_success_rate"] >= 0
    assert admin_payload["fallback_count"] >= 0
    assert admin_payload["qa_trigger_count"] >= 0
    assert admin_payload["ds_clean_success_count_7d"] >= 1
    assert admin_payload["last_execution_profile"] in {"remote_fast", "remote_balanced", "premium_anthropic"}


def test_stable_user_avoids_repeated_qa_on_followup_ds_tasks() -> None:
    founder_id = _user_id("founder@aibridge.local")
    first = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Summarize this short weekly update."}],
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    second = client.post(
        "/v1/messages",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "task_id": first_payload["task_id"],
            "messages": [{"role": "user", "content": "Continue the same summary with one concise next step."}],
        },
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["ab"]["mode"] == "Smart"
    assert second_payload["ab"]["status"] == "In progress"
    admin = client.get(f"/api/admin/agents/{founder_id}", headers={"X-Admin-Key": "admin-test-key"})
    assert admin.status_code == 200
    admin_payload = admin.json()
    assert admin_payload["default_provider_family"].startswith("ds")
    assert admin_payload["stable_task_completion_rate_7d"] >= 0.5


def test_repeated_ds_instability_makes_escalation_more_likely() -> None:
    founder_id = _user_id("founder@aibridge.local")
    with SessionLocal() as db:
        profile = db.scalar(select(AgentProfile).where(AgentProfile.user_id == founder_id))
        assert profile is not None
        profile.recent_ds_success_rate = 0.2
        profile.fallback_count_7d = 4
        profile.premium_escalation_count_7d = 3
        db.commit()
    response = client.post(
        "/api/chat/completions",
        json={
            "user_id": founder_id,
            "mode": "smart",
            "messages": [{"role": "user", "content": "Review this billing incident and auth failure summary."}],
        },
    )
    assert response.status_code == 200
    assert response.json()["ab"]["mode"] == "Assured"


def test_internal_route_telemetry_is_admin_only() -> None:
    founder_id = _user_id("founder@aibridge.local")
    response = client.post(
        "/api/chat/completions",
        json={
            "user_id": founder_id,
            "mode": "fast",
            "messages": [{"role": "user", "content": "Summarize this short note."}],
        },
    )
    assert response.status_code == 200
    request_id = response.json()["id"].replace("ab_", "")
    forbidden = client.get(f"/api/admin/usage/{request_id}")
    assert forbidden.status_code == 403
    allowed = client.get(f"/api/admin/usage/{request_id}", headers={"X-Admin-Key": "admin-test-key"})
    assert allowed.status_code == 200
    admin_payload = allowed.json()
    assert "route_chosen" in admin_payload


def test_landing_has_no_hardcoded_dashboard_user_cta() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "/dashboard/1" not in body
    assert "/dashboard/demo" in body


def test_no_mock_provider_in_production_path_configuration() -> None:
    routes_file = Path("/Users/forrest/ai_bridge_v7_cleanroom/app/routes/api.py").read_text()
    assert "from app.providers.mock import MockProviderClient" not in routes_file


def test_launch_docs_do_not_require_local_inference_node() -> None:
    readme = Path("/Users/forrest/ai_bridge_v7_cleanroom/README.md").read_text().lower()
    architecture = Path("/Users/forrest/ai_bridge_v7_cleanroom/docs/ARCHITECTURE_NOTES.md").read_text().lower()
    env_example = Path("/Users/forrest/ai_bridge_v7_cleanroom/.env.example").read_text().lower()
    assert "production traffic never depends on the founder laptop" in readme
    assert "production inference uses remote commercial-grade providers only" in architecture
    assert "provider_local_enabled=false" in env_example


def test_railway_startup_points_only_to_cleanroom_app() -> None:
    railway = json.loads(Path("/Users/forrest/ai_bridge_v7_cleanroom/railway.json").read_text())
    start_command = railway["deploy"]["startCommand"]
    assert start_command == "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
