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

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.billing import wallet_balance
from app.db import SessionLocal
from app.main import app, bootstrap
from app.models import AgentProfile, ApiKey, PaymentRecord, TaskSession, User
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
    response = client.get("/dashboard/demo")
    assert response.status_code == 200
    body = response.text.lower()
    assert "available now" in body
    assert "step 1: copy api key" in body
    assert "recent top-ups" in body
    assert "recent usage" in body
    assert "feature store" in body


def test_root_and_dashboard_routes_resolve_without_affecting_health() -> None:
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    dashboard = client.get("/dashboard", follow_redirects=True)
    assert dashboard.status_code == 200
    assert "adaptive model routing for developers" in dashboard.text.lower()
    health = client.get("/api/health")
    assert health.status_code == 200


def test_chat_surface_loads_as_product_ui() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "chatuser@example.com", "use_case": "task chat"})
    assert create.status_code == 200
    response = session_client.get("/chat")
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
    assert "right model." in body
    assert "right task." in body
    assert "fewer mistakes." in body
    assert "try 3 free demos" in body
    assert "try 3 free demos →" in body
    assert "adaptive model routing for developers" in body
    assert "demo preview" in body
    assert "dashboard" in body
    assert "get api key" in body
    assert "claude code" not in body
    assert "terminal workflows" in body
    assert "trained model combinations" in body
    assert "preference-aware execution" in body
    assert "your workflow, better directed" in body
    assert 'id="freetrycta"' in body
    assert 'id="chatthread"' in body
    assert "3 free demos included" in body
    assert "$10" in body
    assert "$50" in body
    assert "$200" in body
    assert "$500" in body
    assert "$1,000" in body
    assert "$10 → no bonus" in body
    assert "$50 → includes $5 bonus credit" in body
    assert "$200 → includes $20 bonus credit" in body
    assert "$500 → includes $60 bonus credit" in body
    assert "$1,000 → includes $130 bonus credit" in body
    assert "bill guard" in body
    assert "priority queue" in body
    assert "available now · $20/mo" in body
    assert "early access · seat pricing in product" in body
    assert "early access · limited access" in body
    assert "approval gate" in body
    assert "session memory" in body
    assert "is this a router or an autonomous agent?" in body
    assert "what do starter credit, bonus credit, and rewards actually mean?" in body
    assert "do you train on raw user prompts?" in body
    assert "copy full setup" in body
    assert "your-bridge-key" not in body
    assert "your_key_from_above" not in body
    assert "/privacy" in body
    assert "/terms" in body
    assert "/acceptable-use" in body
    assert 'id="playground"' in body
    assert 'id="pricing"' in body
    assert 'id="modaloverlay"' in body
    assert "/v1/keys" in body
    assert "/demo/chat" in body
    assert "/api/payments/checkout" in body
    assert "starter reward" not in body
    assert '><h3>$10</h3>' not in body


def test_demo_chat_returns_structured_fields_and_enforces_backend_trial_limit() -> None:
    demo_client = TestClient(app)
    first = demo_client.post("/demo/chat", json={"message": "Summarize a heat pump controller spec for me."})
    assert first.status_code == 200
    payload = first.json()
    assert set(payload.keys()) >= {"reply", "lane", "quality", "direct_cost", "routed_cost", "saved_pct", "reason", "trial_remaining", "trial_exhausted", "show_signup_after_ms"}
    assert payload["lane"] in {"Fast", "Smart", "Assured"}
    assert payload["quality"] in {"In progress", "Checked", "Verified"}
    assert payload["direct_cost"].startswith("$")
    assert payload["routed_cost"].startswith("$")
    assert isinstance(payload["saved_pct"], int)
    assert payload["trial_exhausted"] is False
    second = demo_client.post("/demo/chat", json={"example": "refactor"})
    assert second.status_code == 200
    third = demo_client.post("/demo/chat", json={"example": "reply"})
    assert third.status_code == 200
    assert third.json()["trial_exhausted"] is True
    assert third.json()["show_signup_after_ms"] == 7000
    fourth = demo_client.post("/demo/chat", json={"example": "spec"})
    assert fourth.status_code == 429
    assert "anonymous demo limit reached" in fourth.json()["detail"].lower()


def test_demo_chat_still_returns_preview_if_provider_path_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.routes.api._execute_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(status_code=503, detail="selected model does not exist")))
    demo_client = TestClient(app)
    response = demo_client.post("/demo/chat", json={"message": "hello"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"]
    assert payload["lane"] in {"Fast", "Smart", "Assured"}
    assert "selected model" not in response.text.lower()


def test_v1_keys_issues_real_key_and_stores_user_association() -> None:
    response = client.post(
        "/v1/keys",
        json={"email": "newbuilder@example.com", "use_case": "product docs"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key"].startswith("ab_live_")
    assert payload["email"] == "newbuilder@example.com"
    assert payload["dashboard_url"] == "/dashboard"
    assert payload["chat_url"] == "/chat"
    assert payload["granted_credit_usd"] == 3.0
    assert payload["onboarding_commands"][0] == "unset ANTHROPIC_MODEL"
    assert payload["onboarding_commands"][1].startswith('export ANTHROPIC_BASE_URL=')
    assert payload["onboarding_commands"][2].startswith('export ANTHROPIC_API_KEY="ab_live_')
    assert payload["onboarding_commands"][3] == "claude"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "newbuilder@example.com"))
        assert user is not None
        api_keys = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).all()
        assert len(api_keys) == 1
        assert api_keys[0].key_prefix == payload["api_key"][:16]
        assert wallet_balance(db, user.id, "main") == 3.0


def test_issued_api_key_is_usable_for_authenticated_messages_without_user_id() -> None:
    create = client.post(
        "/v1/keys",
        json={"email": "authuser@example.com", "use_case": "release review"},
    )
    assert create.status_code == 200
    create_payload = create.json()
    api_key = create_payload["api_key"]
    with SessionLocal() as db:
        if wallet_balance(db, create_payload["user_id"], "main") <= 0:
            payment = PaymentRecord(
                user_id=create_payload["user_id"],
                pack_code="starter",
                amount_usd=10.0,
                bonus_usd=0.0,
                status="pending",
                stripe_session_id="cs_test_auth_seed",
                referred_by_code=None,
            )
            db.add(payment)
            db.commit()
            process_checkout_completed(db, "evt_auth_seed", "cs_test_auth_seed", "pi_auth_seed")
            db.commit()
    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "mode": "smart",
            "messages": [{"role": "user", "content": "Draft a safe release note for a customer-facing update."}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"]
    assert payload["ab"]["mode"] in {"Smart", "Assured"}


def test_referral_link_and_first_purchase_credit_are_closed_loop_and_one_time() -> None:
    referrer_id = _user_id("user2@example.com")
    with SessionLocal() as db:
        promo_before = wallet_balance(db, referrer_id, "promo")
    referral_page = client.get("/r/UTWO10", follow_redirects=False)
    assert referral_page.status_code == 307
    assert referral_page.headers["location"] == "/?ref=UTWO10"
    key_response = client.post(
        "/v1/keys",
        json={"email": "referredbuilder@example.com", "use_case": "support prompts", "referred_by_code": "UTWO10"},
    )
    assert key_response.status_code == 200
    referred_user_id = key_response.json()["user_id"]
    with SessionLocal() as db:
        referred_user = db.get(User, referred_user_id)
        assert referred_user is not None
        assert referred_user.referred_by_user_id == referrer_id
        first_payment = PaymentRecord(
            user_id=referred_user_id,
            pack_code="starter",
            amount_usd=10.0,
            bonus_usd=0.0,
            status="pending",
            stripe_session_id="cs_test_referral_1",
            referred_by_code=None,
        )
        db.add(first_payment)
        db.commit()
        processed_first = process_checkout_completed(db, "evt_referral_1", "cs_test_referral_1", "pi_referral_1")
        db.commit()
        processed_second = process_checkout_completed(db, "evt_referral_1", "cs_test_referral_1", "pi_referral_1")
        db.commit()
        assert processed_first is True
        assert processed_second is False
        assert wallet_balance(db, referrer_id, "promo") == promo_before + 1.0


def test_dashboard_shows_real_key_balance_and_topup_history_for_created_user() -> None:
    create = client.post(
        "/v1/keys",
        json={"email": "dashboarduser@example.com", "use_case": "release notes"},
    )
    assert create.status_code == 200
    payload = create.json()
    user_id = payload["user_id"]
    with SessionLocal() as db:
        payment = PaymentRecord(
            user_id=user_id,
            pack_code="growth",
            amount_usd=50.0,
            bonus_usd=5.0,
            status="pending",
            stripe_session_id="cs_test_dashboard_1",
            referred_by_code=None,
        )
        db.add(payment)
        db.commit()
        processed = process_checkout_completed(db, "evt_dashboard_1", "cs_test_dashboard_1", "pi_dashboard_1")
        db.commit()
        assert processed is True
    response = client.get(payload["dashboard_url"])
    assert response.status_code == 200
    body = response.text.lower()
    assert "dashboarduser@example.com" in body
    assert payload["api_key"][:16].lower() in body
    assert "$58.00 available now".lower() in body
    assert "growth" in body
    assert "$55.00" in body


def test_dashboard_root_redirects_to_launch_user_after_key_issue() -> None:
    session_client = TestClient(app)
    create = session_client.post(
        "/v1/keys",
        json={"email": "redirectuser@example.com", "use_case": "ops prompts"},
    )
    assert create.status_code == 200
    user_id = create.json()["user_id"]
    response = session_client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/me"
    me_page = session_client.get("/dashboard/me")
    assert me_page.status_code == 200
    assert str(user_id) not in me_page.url.path


def test_dashboard_session_is_email_bound_not_user_id_bound() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "emailbound@example.com", "use_case": "ops"})
    assert create.status_code == 200
    user_id = create.json()["user_id"]
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user is not None
        assert session_client.get("/dashboard/me").status_code == 200
        other = ensure_seed_user(db, "other-dashboard@example.com", "Other Dashboard", referral_code="ODASH")
        db.commit()
        forbidden = session_client.get(f"/dashboard/{other.id}")
        assert forbidden.status_code == 404


def test_checkout_creation_can_bind_credit_to_email_backed_launch_user(monkeypatch) -> None:
    class _FakeSession:
        id = "cs_test_checkout_real"
        url = "https://checkout.stripe.test/session"

    monkeypatch.setattr("app.payments.stripe.checkout.Session.create", lambda **_: _FakeSession())
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "checkoutuser@example.com", "use_case": "topup"})
    assert create.status_code == 200
    response = session_client.post(
        "/api/payments/checkout",
        json={"email": "checkoutuser@example.com", "pack_code": "scale_plus", "referred_by_code": "UTWO10"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["checkout_url"] == "https://checkout.stripe.test/session"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "checkoutuser@example.com"))
        assert user is not None
        assert user.referred_by_user_id == _user_id("user2@example.com")
        payment = db.scalar(select(PaymentRecord).where(PaymentRecord.stripe_session_id == "cs_test_checkout_real"))
        assert payment is not None
        assert payment.user_id == user.id
        assert payment.pack_code == "scale_plus"
        assert payment.amount_usd == 500.0
        assert user.email == "checkoutuser@example.com"


def test_checkout_is_blocked_for_inherited_admin_or_seed_identity(monkeypatch) -> None:
    class _FakeSession:
        id = "cs_should_not_exist_admin"
        url = "https://checkout.stripe.test/session"

    monkeypatch.setattr("app.payments.stripe.checkout.Session.create", lambda **_: _FakeSession())
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "Bernard.gmny@gmail.com", "name": "Bernard"})
    assert create.status_code == 200
    response = session_client.post(
        "/api/payments/checkout",
        json={"email": "Bernard.gmny@gmail.com", "pack_code": "starter"},
    )
    assert response.status_code == 403
    assert "launch verification" in response.json()["detail"].lower()


def test_checkout_is_blocked_without_authenticated_launch_session(monkeypatch) -> None:
    class _FakeSession:
        id = "cs_should_not_exist"
        url = "https://checkout.stripe.test/session"

    monkeypatch.setattr("app.payments.stripe.checkout.Session.create", lambda **_: _FakeSession())
    isolated = TestClient(app)
    response = isolated.post(
        "/api/payments/checkout",
        json={"email": "unsafe@example.com", "pack_code": "starter"},
    )
    assert response.status_code == 403
    assert "temporarily unavailable during launch verification" in response.json()["detail"].lower()


def test_admin_dashboard_route_shows_aggregate_metrics() -> None:
    response = client.get("/admin/dashboard", headers={"X-Admin-Key": "admin-test-key"})
    assert response.status_code == 200
    body = response.text.lower()
    assert "admin dashboard" in body
    assert "trial to signup conversion" in body
    assert "signup to first top-up conversion" in body
    assert "recent failures" in body


def test_webhook_processing_is_idempotent() -> None:
    founder_id = _user_id("founder@aibridge.local")
    referrer_id = _user_id("user2@example.com")
    with SessionLocal() as db:
        promo_before = wallet_balance(db, referrer_id, "promo")
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
        assert wallet_balance(db, referrer_id, "promo") == promo_before + 5.0


def test_volume_pack_webhook_credits_expected_balance_once() -> None:
    founder_id = _user_id("founder@aibridge.local")
    with SessionLocal() as db:
        payment = PaymentRecord(
            user_id=founder_id,
            pack_code="volume",
            amount_usd=1000.0,
            bonus_usd=130.0,
            status="pending",
            stripe_session_id="cs_test_volume_1",
            referred_by_code=None,
        )
        db.add(payment)
        db.commit()
        processed_first = process_checkout_completed(db, "evt_volume_1", "cs_test_volume_1", "pi_volume_1")
        db.commit()
        processed_second = process_checkout_completed(db, "evt_volume_1", "cs_test_volume_1", "pi_volume_1")
        db.commit()
        assert processed_first is True
        assert processed_second is False
        assert wallet_balance(db, founder_id, "main") >= 1185.0


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
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "threaduser@example.com", "use_case": "chat"})
    assert create.status_code == 200
    founder_id = create.json()["user_id"]
    first = session_client.post(
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
    second = session_client.post(
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
    thread = session_client.get(f"/api/tasks/{founder_id}/{task_id}")
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
    isolated = TestClient(app)
    response = isolated.get("/")
    assert response.status_code == 200
    body = response.text
    assert "/dashboard/1" not in body
    assert 'href="/dashboard"' not in body


def test_privacy_page_exists_and_is_linked() -> None:
    landing = client.get("/")
    assert landing.status_code == 200
    assert "/privacy" in landing.text
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert "privacy policy" in privacy.text.lower()
    assert "de-identified operational analytics" in privacy.text.lower()
    assert "prompt and content handling" in privacy.text.lower()
    assert "sections" in privacy.text.lower()
    assert "retention and security" in privacy.text.lower()
    terms = client.get("/terms")
    assert terms.status_code == 200
    assert "credits, bonuses, and rewards" in terms.text.lower()
    assert "add-ons and feature unlocks" in terms.text.lower()
    assert "payments and ledger correctness" in terms.text.lower()
    acceptable = client.get("/acceptable-use")
    assert acceptable.status_code == 200
    assert "security and abuse prevention" in acceptable.text.lower()
    assert "fair use of the platform" in acceptable.text.lower()


def test_dashboard_user_id_route_is_not_open_for_enumeration() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "guarduser@example.com"})
    assert create.status_code == 200
    response = session_client.get("/dashboard/999999")
    assert response.status_code == 404


def test_admin_dashboard_requires_header_or_admin_cookie_not_query_param() -> None:
    isolated = TestClient(app)
    forbidden = isolated.get("/admin/dashboard?key=admin-test-key")
    assert forbidden.status_code == 403
    session_client = TestClient(app)
    allowed = session_client.get("/admin/dashboard", headers={"X-Admin-Key": "admin-test-key"})
    assert allowed.status_code == 200
    cookie_access = session_client.get("/admin/dashboard")
    assert cookie_access.status_code == 200


def test_bernard_email_session_has_admin_access() -> None:
    session_client = TestClient(app)
    signup = session_client.post("/v1/keys", json={"email": "Bernard.gmny@gmail.com", "name": "Bernard"})
    assert signup.status_code == 200
    admin = session_client.get("/admin/dashboard")
    assert admin.status_code == 200
    assert "admin dashboard" in admin.text.lower()


def test_alias_models_are_accepted_without_raw_model_errors() -> None:
    create = client.post("/v1/keys", json={"email": "aliasuser@example.com", "use_case": "editor flow"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "Review this release note."}],
        },
    )
    assert response.status_code == 200
    assert "selected model does not exist" not in response.text.lower()


def test_outer_compat_boundary_rewrites_chat_completion_alias_before_deeper_path(monkeypatch) -> None:
    create = client.post("/v1/keys", json={"email": "compatchat@example.com", "use_case": "terminal"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    captured: dict[str, object] = {}

    def _fake_chat(payload, request, authorization, x_api_key, db, settings):
        captured["path"] = str(request.url.path)
        captured["mode"] = payload.mode
        captured["model"] = payload.model
        return {
            "id": "ab_test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "ab": {"mode": "Assured", "status": "In progress", "billing": {"public_charge_usd": 0.0, "balance_remaining_usd": 3.0}},
        }

    monkeypatch.setattr("app.routes.api.api_chat_completions", _fake_chat)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert captured["path"] == "/v1/chat/completions"
    assert captured["mode"] == "assured"
    assert captured["model"] is None


def test_outer_compat_boundary_rewrites_messages_alias_before_deeper_path(monkeypatch) -> None:
    create = client.post("/v1/keys", json={"email": "compatmessages@example.com", "use_case": "terminal"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    captured: dict[str, object] = {}

    def _fake_messages(payload, request, authorization, x_api_key, db, settings):
        captured["path"] = str(request.url.path)
        captured["mode"] = payload.mode
        captured["model"] = payload.model
        return {
            "id": "ab_test",
            "type": "message",
            "role": "assistant",
            "task_id": "task_test",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "ab": {"mode": "Assured", "status": "In progress", "task_state": "Verified", "billing": {"public_charge_usd": 0.0, "balance_remaining_usd": 3.0}},
            "task": {"task_id": "task_test"},
        }

    monkeypatch.setattr("app.routes.api.api_messages", _fake_messages)
    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert captured["path"] == "/v1/messages"
    assert captured["mode"] == "assured"
    assert captured["model"] is None


def test_terminal_hello_flow_uses_alias_without_exposing_model_errors() -> None:
    create = client.post("/v1/keys", json={"email": "helloalias@example.com", "use_case": "terminal hello"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "claude-3-7-sonnet-latest",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.text.lower()
    assert "selected model does not exist" not in body
    assert "service temporarily unavailable" not in body


def test_terminal_hello_messages_flow_uses_alias_without_vendor_leakage() -> None:
    create = client.post("/v1/keys", json={"email": "hellomessages@example.com", "use_case": "terminal hello messages"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.text.lower()
    assert "selected model does not exist" not in body
    assert "claude-sonnet-4-5" not in body
    assert "anthropic" not in body


def test_outer_compat_boundary_returns_only_neutral_message_on_model_failure(monkeypatch) -> None:
    create = client.post("/v1/keys", json={"email": "neutralerror@example.com", "use_case": "terminal"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]

    def _fail_chat(*args, **kwargs):
        raise HTTPException(status_code=400, detail="There's an issue with the selected model (claude-sonnet-4-6). It may not exist or you may not have access.")

    monkeypatch.setattr("app.routes.api.api_chat_completions", _fail_chat)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 503
    body = response.text.lower()
    assert "temporarily unavailable" in body
    assert "selected model" not in body
    assert "claude-sonnet-4-6" not in body


def test_dashboard_matches_landing_user_blocks() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "dashstyle@example.com", "use_case": "ops"})
    assert create.status_code == 200
    response = session_client.get("/dashboard")
    assert response.status_code == 200
    body = response.text.lower()
    assert "available now" in body
    assert "added this month" in body
    assert "used this month" in body
    assert "bonus posted" in body
    assert "rewards posted" in body
    assert "step 1: copy api key" in body
    assert "step 2: copy setup commands" in body
    assert "step 3: run in terminal" in body
    assert "recent top-ups" in body
    assert "recent sessions" in body
    assert "referral" in body
    assert "https://getaibridge.com/signup?ref=" in body
    assert "feature store" in body
    assert "priority queue" in body
    assert "$20.00/mo" in body
    assert "approval gate" in body
    assert "session memory" in body
    assert "your_key_from_above" not in body
    assert "copy full setup" in body


def test_dashboard_setup_commands_use_real_key_for_signed_user() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "realsetup@example.com", "use_case": "terminal"})
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    page = session_client.get("/dashboard")
    assert page.status_code == 200
    body = page.text.lower()
    assert api_key in page.text
    assert 'unset anthropic_model' in body
    assert 'export anthropic_base_url=' in body
    assert 'https://getaibridge.com/v1' in body
    assert 'export anthropic_api_key=' in body
    assert api_key in page.text
    assert "your_key_from_above" not in body
    assert "available now" in body
    assert "early access" in body


def test_home_logo_links_back_to_root_on_live_surfaces() -> None:
    landing = client.get("/")
    assert landing.status_code == 200
    assert 'href="/" class="logo"' in landing.text

    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "logouser@example.com", "use_case": "home link"})
    assert create.status_code == 200
    dashboard = session_client.get("/dashboard")
    assert dashboard.status_code == 200
    assert 'href="/" class="logo"' in dashboard.text


def test_chat_surface_feels_like_core_product_shell() -> None:
    session_client = TestClient(app)
    create = session_client.post("/v1/keys", json={"email": "chatux@example.com", "use_case": "editor"})
    assert create.status_code == 200
    response = session_client.get("/chat")
    assert response.status_code == 200
    body = response.text.lower()
    assert "new task" in body
    assert "task status stays visible." in body
    assert "quick-action" in body
    assert "recent messages visible" not in body
    assert "approval gate" not in body


def test_admin_dashboard_uses_shared_product_shell_styles() -> None:
    response = client.get("/admin/dashboard", headers={"X-Admin-Key": "admin-test-key"})
    assert response.status_code == 200
    body = response.text.lower()
    assert "admin dashboard" in body
    assert "total users" in body
    assert "funnel" in body


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
