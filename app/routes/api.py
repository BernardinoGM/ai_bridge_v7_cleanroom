import uuid

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.benchmark import benchmark_cost_usd
from app.billing import debit_usage, wallet_balance
from app.config import Settings, get_settings
from app.costing import estimate_serving_cost_usd
from app.dashboard import build_dashboard
from app.db import get_db
from app.agents import choose_initial_lane, get_or_create_agent_profile, update_profile_after_turn
from app.api_keys import authenticate_api_key, issue_api_key, attach_referrer_by_code
from app.models import AgentProfile, DemoTrial, RequestFailure, TaskSession, TaskTurn, UsageEvent, User
from app.payments import create_checkout_session, ensure_seed_user, process_checkout_completed
from app.pricing import TOP_UP_PACKS, estimate_public_charge
from app.providers.base import ProviderClient, ProviderResponse
from app.providers.real import ProviderExecutionError, build_provider_clients
from app.providers.mock import build_mock_clients
from app.routing import RouteDecision, decide_route
from app.schemas import ApiKeyCreateRequest, ChatCompletionRequest, CheckoutCreateRequest, DemoChatRequest, MessagesRequest
from app.tasks import record_task_turn, resolve_task


router = APIRouter(prefix="/api")
compat_router = APIRouter(prefix="/v1")
demo_router = APIRouter()

DEMO_COOKIE_NAME = "ab_demo_session"
DEMO_TRIAL_LIMIT = 3
LAUNCH_USER_COOKIE_NAME = "ab_launch_user"


DEMO_EXAMPLES = {
    "spec": {
        "prompt": "Summarize a 6-page product spec and keep the follow-up thread stable across revisions.",
        "title": "Summarize a spec",
    },
    "refactor": {
        "prompt": "Refactor a file, preserve intent across edits, and flag only the points that truly need deeper review.",
        "title": "Refactor a file",
    },
    "reply": {
        "prompt": "Draft a customer reply that is calm, clear, and safe to send without unnecessary premium reasoning.",
        "title": "Draft a customer reply",
    },
}


def _task_status_label(task: TaskSession) -> str:
    if task.last_status_label:
        return task.last_status_label
    if task.pinned_lane == "assured":
        return "Verified"
    return "Checked" if task.quality_check_enabled else "In progress"


def _human_status(status: str) -> str:
    normalized = status.replace("_", " ").strip().lower()
    if normalized == "in progress":
        return "In progress"
    if normalized == "checked":
        return "Checked"
    if normalized == "verified":
        return "Verified"
    return normalized.title()


def _serialize_task_summary(task: TaskSession) -> dict:
    return {
        "task_id": task.task_id,
        "title": task.title or "New task",
        "summary": task.summary or task.last_user_message or "",
        "mode": task.pinned_lane.title(),
        "status": _task_status_label(task),
        "archived": task.archived,
        "starred": task.starred,
        "turn_count": task.turn_count,
        "updated_at": task.updated_at.isoformat(),
        "last_assistant_excerpt": task.last_assistant_excerpt or "",
        "source_surface": task.source_surface or "api",
    }


def _serialize_task_thread(task: TaskSession, turns: list[TaskTurn]) -> dict:
    messages: list[dict] = []
    for turn in turns:
        messages.append(
            {
                "id": f"user_{turn.request_id}",
                "role": "user",
                "content": turn.user_message,
                "status": _human_status(turn.status_label),
                "created_at": turn.created_at.isoformat(),
            }
        )
        messages.append(
            {
                "id": f"assistant_{turn.request_id}",
                "role": "assistant",
                "content": turn.assistant_excerpt or "",
                "status": "Verified" if task.pinned_lane == "assured" else ("Checked" if turn.quality_checked else "In progress"),
                "created_at": turn.created_at.isoformat(),
            }
        )
    return {
        "task": _serialize_task_summary(task),
        "messages": messages,
    }


def _provider_registry(settings: Settings) -> dict[str, ProviderClient]:
    if settings.provider_mock_enabled or settings.app_env == "testing":
        return build_mock_clients()
    return build_provider_clients(settings)


def _default_demo_profile() -> AgentProfile:
    return AgentProfile(
        user_id=0,
        preferred_mode="smart",
        default_provider_family="ds_balanced",
        workload_pattern="general",
        escalation_sensitivity="balanced",
        qa_preference="adaptive",
        cost_guardrail_band="standard",
        stable_task_bias="enabled",
        pacing_context="steady",
        last_successful_provider="remote_balanced",
        recent_premium_trigger_count=0,
        recent_ds_success_rate=1.0,
        fallback_count=0,
        qa_trigger_count=0,
        fallback_count_7d=0,
        qa_trigger_rate_7d=0.0,
        stable_task_completion_rate_7d=1.0,
        ds_clean_success_count_7d=0,
        premium_escalation_count_7d=0,
        last_execution_profile="remote_balanced",
        learned_hints_json={},
    )


def _format_usd(amount: float) -> str:
    return f"${amount:.2f}"


def _display_quality(route: RouteDecision) -> str:
    if route.premium_escalated:
        return "Verified"
    if route.quality_check:
        return "Checked"
    return "In progress"


def _demo_reason(example: str, route: RouteDecision) -> str:
    if route.premium_escalated:
        return "Escalated because this example carries higher review or release risk."
    if example == "refactor":
        return "Stayed cheaper because the work is iterative and the thread can stay stable without premium on every turn."
    if example == "reply":
        return "Stayed cheaper because the request is straightforward and low risk."
    return "Stayed cheaper because the request is routine and the thread can remain stable across follow-ups."


def _get_or_create_demo_trial(db: Session, session_id: str) -> DemoTrial:
    trial = db.scalar(select(DemoTrial).where(DemoTrial.session_id == session_id))
    if trial:
        return trial
    trial = DemoTrial(session_id=session_id, tries_used=0)
    db.add(trial)
    db.flush()
    return trial


def _execute_with_fallback(
    route: RouteDecision,
    registry: dict[str, ProviderClient],
    prompt: str,
    system: str | None,
) -> tuple[ProviderResponse, str, bool]:
    primary_key = route.execution_profile
    fallback_key = "remote_balanced" if primary_key == "premium_anthropic" else "premium_anthropic"
    if primary_key not in registry:
        raise HTTPException(status_code=503, detail="Provider execution profile is unavailable.")
    try:
        return registry[primary_key].generate(prompt=prompt, system=system), primary_key, False
    except ProviderExecutionError:
        if fallback_key not in registry:
            raise HTTPException(status_code=503, detail="No fallback provider available.")
        fallback_response = registry[fallback_key].generate(prompt=prompt, system=system)
        return fallback_response, fallback_key, True


def _route_preview(
    *,
    prompt: str,
    mode: str,
    settings: Settings,
    system: str | None = None,
    profile: AgentProfile | None = None,
) -> dict:
    effective_profile = profile or _default_demo_profile()
    visible_lane, internal_lane, quality_check = choose_initial_lane(effective_profile, mode, prompt)
    route = decide_route(prompt, visible_lane, internal_lane=internal_lane, quality_check_override=quality_check)
    registry = _provider_registry(settings)
    provider_response, execution_profile_used, fallback_used = _execute_with_fallback(route, registry, prompt, system)
    routed_cost = estimate_public_charge(
        mode=visible_lane,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=route.quality_check,
    )
    direct_premium_cost = estimate_public_charge(
        mode="assured",
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=True,
    )
    saved_pct = max(0, round((1 - (routed_cost / max(direct_premium_cost, 0.01))) * 100))
    return {
        "reply": provider_response.text,
        "lane": visible_lane.title(),
        "quality": _display_quality(route),
        "direct_cost": _format_usd(direct_premium_cost),
        "routed_cost": _format_usd(routed_cost),
        "saved_pct": saved_pct,
        "why": _demo_reason("spec", route),
        "execution_profile": execution_profile_used,
        "fallback_used": fallback_used,
        "premium_escalated": route.premium_escalated,
    }


def _require_admin_key(x_admin_key: str | None, settings: Settings) -> None:
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Admin access required.")


def _record_failure(
    db: Session,
    endpoint: str,
    error_message: str,
    user_id: int | None = None,
    context_json: dict | None = None,
) -> None:
    db.add(
        RequestFailure(
            endpoint=endpoint,
            user_id=user_id,
            error_message=error_message[:255],
            context_json=context_json,
        )
    )
    db.commit()


def _cookie_user_id(request: Request) -> int | None:
    raw = request.cookies.get(LAUNCH_USER_COOKIE_NAME)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_user_id(
    db: Session,
    settings: Settings,
    request: Request,
    payload_user_id: int | None,
    authorization: str | None = None,
    x_api_key: str | None = None,
) -> int:
    if payload_user_id is not None:
        return payload_user_id
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    user = authenticate_api_key(db, settings, x_api_key or bearer)
    if user is not None:
        return user.id
    cookie_user_id = _cookie_user_id(request)
    if cookie_user_id is not None and db.get(User, cookie_user_id) is not None:
        return cookie_user_id
    raise HTTPException(status_code=401, detail="Authentication required. Provide a valid API key or launch session.")


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "vpn_required": False,
    }


@demo_router.post("/demo/chat")
def demo_chat(
    payload: DemoChatRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    session_id = request.cookies.get(DEMO_COOKIE_NAME) or uuid.uuid4().hex
    trial = _get_or_create_demo_trial(db, session_id)
    if trial.tries_used >= DEMO_TRIAL_LIMIT:
        raise HTTPException(status_code=429, detail="Anonymous demo limit reached. Open the dashboard demo or request API access to continue.")
    example_key = payload.example or "spec"
    prompt = (payload.message or "").strip()
    if not prompt:
        prompt = DEMO_EXAMPLES[example_key]["prompt"]
    try:
        preview = _route_preview(prompt=prompt[:8000], mode="smart", settings=settings)
        trial.tries_used += 1
        trial.last_example = example_key
        trial.last_prompt_excerpt = prompt[:255]
        db.commit()
        response.set_cookie(
            key=DEMO_COOKIE_NAME,
            value=session_id,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
        )
        reason = _demo_reason(example_key, decide_route(prompt, "smart"))
        preview["reason"] = reason
        preview["why"] = reason
        preview["trial_remaining"] = max(0, DEMO_TRIAL_LIMIT - trial.tries_used)
        preview["tries_remaining"] = preview["trial_remaining"]
        preview["trial_exhausted"] = trial.tries_used >= DEMO_TRIAL_LIMIT
        preview["show_signup_after_ms"] = 7000 if preview["trial_exhausted"] else 0
        return preview
    except HTTPException as exc:
        _record_failure(db, "/demo/chat", exc.detail if isinstance(exc.detail, str) else "demo failure", context_json={"example": example_key})
        raise


@compat_router.post("/keys")
def create_api_key_launch(
    payload: ApiKeyCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        user, raw_key, granted_credit, balance_usd = issue_api_key(
            db=db,
            settings=settings,
            email=payload.email,
            name=payload.name,
            use_case=payload.use_case,
            referred_by_code=payload.referred_by_code,
        )
        db.commit()
        response.set_cookie(
            key=LAUNCH_USER_COOKIE_NAME,
            value=str(user.id),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
        )
        return {
            "api_key": raw_key,
            "user_id": user.id,
            "email": user.email,
            "granted_credit_usd": round(granted_credit, 2),
            "balance_usd": round(balance_usd, 2),
            "dashboard_url": f"/dashboard/{user.id}",
            "chat_url": f"/chat/{user.id}",
            "onboarding_commands": [
                'export ANTHROPIC_BASE_URL="https://getaibridge.com/v1"',
                f'export ANTHROPIC_API_KEY="{raw_key}"',
                "claude",
            ],
        }
    except HTTPException as exc:
        _record_failure(db, "/v1/keys", exc.detail if isinstance(exc.detail, str) else "signup failed", context_json={"email": payload.email})
        raise


@router.get("/topups/packs")
def list_topup_packs() -> dict:
    return {
        "packs": [
            {
                "code": pack.code,
                "name": pack.name,
                "price_usd": pack.price_usd,
                "bonus_usd": pack.bonus_usd,
                "tagline": pack.tagline,
                "governance_note": pack.governance_note,
            }
            for pack in TOP_UP_PACKS.values()
        ]
    }


@router.post("/payments/checkout")
def create_checkout(
    payload: CheckoutCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        resolved_user_id = payload.user_id
        if payload.email:
            user = ensure_seed_user(
                db,
                email=payload.email.strip().lower(),
                name=payload.email.split("@", 1)[0].replace(".", " ").replace("_", " ").title() or "AI Bridge User",
            )
            attach_referrer_by_code(db, user, payload.referred_by_code)
            resolved_user_id = user.id
        elif resolved_user_id is None:
            cookie_user_id = _cookie_user_id(request)
            if cookie_user_id is not None and db.get(User, cookie_user_id) is not None:
                resolved_user_id = cookie_user_id
        if resolved_user_id is None:
            raise HTTPException(status_code=400, detail="Email is required to start checkout before full sign-in.")
        response.set_cookie(
            key=LAUNCH_USER_COOKIE_NAME,
            value=str(resolved_user_id),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
        )
        result = create_checkout_session(
            db=db,
            settings=settings,
            user_id=resolved_user_id,
            pack_code=payload.pack_code,
            referred_by_code=payload.referred_by_code,
        )
        db.commit()
        return {"checkout_url": result.checkout_url, "session_id": result.session_id, "user_id": resolved_user_id}
    except HTTPException as exc:
        _record_failure(db, "/api/payments/checkout", exc.detail if isinstance(exc.detail, str) else "checkout failed", context_json={"pack_code": payload.pack_code})
        raise


@router.post("/payments/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(alias="Stripe-Signature"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=stripe_signature, secret=settings.stripe_webhook_secret)
    except ValueError as exc:
        _record_failure(db, "/api/payments/webhook", "Invalid payload")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        _record_failure(db, "/api/payments/webhook", "Invalid signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature") from exc
    if event["type"] == "checkout.session.completed":
        session_data = event["data"]["object"]
        try:
            processed = process_checkout_completed(
                db=db,
                event_id=event["id"],
                stripe_session_id=session_data["id"],
                stripe_payment_intent_id=session_data.get("payment_intent"),
            )
            db.commit()
            return JSONResponse({"processed": processed})
        except Exception as exc:
            _record_failure(db, "/api/payments/webhook", str(exc), context_json={"session_id": session_data.get("id")})
            raise
    return JSONResponse({"processed": False, "ignored": event["type"]})


@router.get("/dashboard/{user_id}")
def dashboard_api(user_id: int, db: Session = Depends(get_db)) -> dict:
    data = build_dashboard(db, user_id)
    return {
        "balance_usd": data["balance_usd"],
        "days_left": data["days_left"],
        "heavy_workdays_left": data["heavy_workdays_left"],
        "premium_savings_estimate_usd": data["premium_savings_estimate_usd"],
        "mode_estimates": [
            {"mode": item.mode, "days_left": item.days_left, "heavy_workdays_left": item.heavy_workdays_left}
            for item in data["mode_estimates"]
        ],
        "upsells": data["upsells"],
    }


@router.get("/tasks/{user_id}")
def list_tasks(user_id: int, archived: bool = False, db: Session = Depends(get_db)) -> dict:
    tasks = db.scalars(
        select(TaskSession)
        .where(TaskSession.user_id == user_id, TaskSession.archived == archived)
        .order_by(TaskSession.starred.desc(), desc(TaskSession.updated_at))
    ).all()
    dashboard = build_dashboard(db, user_id)
    return {
        "tasks": [_serialize_task_summary(task) for task in tasks],
        "runway": {
            "balance_usd": dashboard["balance_usd"],
            "days_left": dashboard["days_left"],
            "heavy_workdays_left": dashboard["heavy_workdays_left"],
        },
    }


@router.get("/tasks/{user_id}/{task_id}")
def get_task_thread(user_id: int, task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(TaskSession).where(TaskSession.user_id == user_id, TaskSession.task_id == task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    turns = db.scalars(
        select(TaskTurn).where(TaskTurn.task_session_id == task.id).order_by(TaskTurn.created_at.asc(), TaskTurn.id.asc())
    ).all()
    return _serialize_task_thread(task, turns)


@router.get("/admin/usage/{request_id}")
def admin_usage_event(
    request_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    _require_admin_key(x_admin_key, settings)
    event = db.query(UsageEvent).filter(UsageEvent.request_id == request_id).first()
    if event is None:
        raise HTTPException(status_code=404, detail="Usage event not found.")
    return {
        "request_id": event.request_id,
        "task_id": event.task_id,
        "mode": event.mode,
        "route_chosen": event.route_chosen,
        "premium_escalated": event.premium_escalated,
        "local_model_hit": event.local_model_hit,
        "fallback_used": event.fallback_used,
        "quality_check_triggered": event.quality_check_triggered,
        "benchmark_cost_usd": event.benchmark_cost_usd,
        "serving_cogs_usd": event.serving_cogs_usd,
    }


@router.get("/admin/agents/{user_id}")
def admin_agent_profile(
    user_id: int,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    _require_admin_key(x_admin_key, settings)
    profile = db.query(AgentProfile).filter(AgentProfile.user_id == user_id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Agent profile not found.")
    return {
        "user_id": profile.user_id,
        "preferred_mode": profile.preferred_mode,
        "default_provider_family": profile.default_provider_family,
        "last_successful_provider": profile.last_successful_provider,
        "recent_premium_trigger_count": profile.recent_premium_trigger_count,
        "recent_ds_success_rate": profile.recent_ds_success_rate,
        "fallback_count": profile.fallback_count,
        "qa_trigger_count": profile.qa_trigger_count,
        "fallback_count_7d": profile.fallback_count_7d,
        "qa_trigger_rate_7d": profile.qa_trigger_rate_7d,
        "stable_task_completion_rate_7d": profile.stable_task_completion_rate_7d,
        "ds_clean_success_count_7d": profile.ds_clean_success_count_7d,
        "premium_escalation_count_7d": profile.premium_escalation_count_7d,
        "last_execution_profile": profile.last_execution_profile,
        "learned_hints_json": profile.learned_hints_json,
    }


def _complete_chat(
    user_id: int,
    mode: str,
    system: str | None,
    prompt: str,
    db: Session,
    settings: Settings,
    endpoint: str,
    task_id: str | None = None,
    pinned_internal_lane: str | None = None,
    pinned_provider: str | None = None,
    pinned_execution_profile: str | None = None,
    quality_check_override: bool | None = None,
) -> dict:
    route = decide_route(prompt, mode, internal_lane=pinned_internal_lane, quality_check_override=quality_check_override)
    provider_override_map = {
        "fast_lane": "remote_fast",
        "balanced_lane": "remote_balanced",
        "premium_reasoner": "premium_anthropic",
    }
    if pinned_provider and pinned_provider in provider_override_map:
        route = RouteDecision(
            provider=pinned_provider,
            provider_model=route.provider_model,
            premium_escalated=pinned_provider == "premium_reasoner",
            quality_check=route.quality_check,
            fallback_provider=route.fallback_provider,
            local_model_hit=False,
            execution_profile=provider_override_map[pinned_provider],
        )
    elif pinned_execution_profile:
        route = RouteDecision(
            provider=pinned_provider or route.provider,
            provider_model=route.provider_model,
            premium_escalated=route.premium_escalated,
            quality_check=route.quality_check,
            fallback_provider=route.fallback_provider,
            local_model_hit=False,
            execution_profile=pinned_execution_profile,
        )
    registry = _provider_registry(settings)
    provider_response, execution_profile_used, fallback_used = _execute_with_fallback(route, registry, prompt, system)
    public_charge = estimate_public_charge(
        mode=mode,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=route.quality_check,
    )
    if wallet_balance(db, user_id, "main") < public_charge:
        raise HTTPException(status_code=402, detail="Insufficient main balance. Top up to continue.")
    cost_estimate = estimate_serving_cost_usd(
        provider_key=execution_profile_used,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        public_charge_usd=public_charge,
        quality_check=route.quality_check,
        fallback_used=fallback_used,
        retry_count=provider_response.retry_count,
    )
    benchmark = benchmark_cost_usd(provider_response.prompt_tokens_est, provider_response.completion_tokens_est, settings)
    request_id = uuid.uuid4().hex
    debit_usage(db, user_id=user_id, amount_usd=public_charge, request_id=request_id, mode=mode)
    event = UsageEvent(
        user_id=user_id,
        task_id=task_id,
        request_id=request_id,
        endpoint=endpoint,
        mode=mode,
        public_charge_usd=public_charge,
        serving_cogs_usd=cost_estimate.serving_cogs_usd,
        benchmark_cost_usd=benchmark,
        gross_margin_guardrail_usd=cost_estimate.guardrail_usd,
        route_chosen=execution_profile_used,
        premium_escalated=route.premium_escalated,
        local_model_hit=False,
        fallback_used=fallback_used,
        retry_count=provider_response.retry_count,
        quality_check_triggered=route.quality_check,
        latency_ms=provider_response.latency_ms,
        prompt_tokens_est=provider_response.prompt_tokens_est,
        completion_tokens_est=provider_response.completion_tokens_est,
        request_payload={"prompt": prompt[:500], "mode": mode},
        response_excerpt=provider_response.text[:200],
    )
    db.add(event)
    db.commit()
    return {
        "id": f"ab_{request_id}",
        "content": provider_response.text,
        "mode": mode,
        "request_id": request_id,
        "provider_family": execution_profile_used,
        "fallback_used": fallback_used,
        "usage": {
            "prompt_tokens": provider_response.prompt_tokens_est,
            "completion_tokens": provider_response.completion_tokens_est,
        },
        "billing": {
            "public_charge_usd": public_charge,
            "balance_remaining_usd": wallet_balance(db, user_id, "main"),
        },
        "telemetry": {"premium_escalated": route.premium_escalated, "quality_check_triggered": route.quality_check, "local_model_hit": False},
    }


@router.post("/chat/completions")
def api_chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if payload.stream:
        raise HTTPException(status_code=400, detail="Streaming is disabled in launch mode for accurate billing.")
    user_id = _resolve_user_id(db, settings, request, payload.user_id, authorization, x_api_key)
    system = next((message.content for message in payload.messages if message.role == "system"), None)
    prompt = "\n".join(message.content for message in payload.messages if message.role == "user")
    profile = get_or_create_agent_profile(db, user_id)
    visible_lane, internal_lane, quality_check = choose_initial_lane(profile, payload.mode, prompt)
    result = _complete_chat(
        user_id,
        visible_lane,
        system,
        prompt,
        db,
        settings,
        "/v1/chat/completions",
        pinned_internal_lane=internal_lane,
        quality_check_override=quality_check,
    )
    update_profile_after_turn(
        profile=profile,
        task_id=result["request_id"],
        visible_lane=visible_lane,
        quality_checked=result["telemetry"]["quality_check_triggered"],
        provider_family=result["provider_family"],
        execution_profile=result["provider_family"],
        premium_escalated=result["telemetry"]["premium_escalated"],
        fallback_used=result["fallback_used"],
        task_stable=not result["fallback_used"] and not result["telemetry"]["premium_escalated"],
        ds_succeeded_cleanly=result["provider_family"] != "premium_anthropic" and not result["fallback_used"],
    )
    db.commit()
    return {
        "id": result["id"],
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]}, "finish_reason": "stop"}],
        "usage": result["usage"],
        "ab": {"mode": visible_lane.title(), "status": "Checked" if result["telemetry"]["quality_check_triggered"] else "In progress", "billing": result["billing"]},
    }


@router.post("/messages")
def api_messages(
    payload: MessagesRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if payload.stream:
        raise HTTPException(status_code=400, detail="Streaming is disabled in launch mode for accurate billing.")
    user_id = _resolve_user_id(db, settings, request, payload.user_id, authorization, x_api_key)
    prompt = "\n".join(str(item.get("content", "")) for item in payload.messages if item.get("role") == "user")
    task = resolve_task(
        db,
        user_id,
        payload.mode,
        prompt,
        payload.task_id,
        payload.task_action,
        source_surface=payload.source_surface,
    )
    result = _complete_chat(
        user_id,
        task.pinned_lane,
        payload.system,
        prompt,
        db,
        settings,
        "/v1/messages",
        task_id=task.task_id,
        pinned_internal_lane=task.internal_lane,
        pinned_provider=task.pinned_provider,
        pinned_execution_profile=task.pinned_execution_profile,
        quality_check_override=task.quality_check_enabled,
    )
    correction_terms = ["verify", "recheck", "double-check", "double check", "correct", "fix again", "audit", "bugfix", "bug fix", "refactor", "regression", "patch"]
    task_stable = (
        not result["fallback_used"]
        and not result["telemetry"]["premium_escalated"]
        and not any(term in prompt.lower() for term in correction_terms)
        and task.continuity_status != "degraded"
    )
    record_task_turn(
        db,
        task,
        result["request_id"],
        prompt,
        result["content"],
        result["telemetry"]["quality_check_triggered"],
        result["provider_family"],
        result["provider_family"],
        result["telemetry"]["premium_escalated"],
        result["fallback_used"],
        task_stable,
        payload.source_surface,
    )
    db.commit()
    return {
        "id": result["id"],
        "type": "message",
        "role": "assistant",
        "task_id": task.task_id,
        "content": [{"type": "text", "text": result["content"]}],
        "usage": result["usage"],
        "ab": {
            "mode": task.pinned_lane.title(),
            "status": "Checked" if result["telemetry"]["quality_check_triggered"] else "In progress",
            "task_state": "Verified" if task.pinned_lane == "assured" else "In progress",
            "billing": result["billing"],
        },
        "task": _serialize_task_summary(task),
    }


@compat_router.post("/chat/completions")
def v1_chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return api_chat_completions(
        payload=payload,
        request=request,
        authorization=authorization,
        x_api_key=x_api_key,
        db=db,
        settings=settings,
    )


@compat_router.post("/messages")
def v1_messages(
    payload: MessagesRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return api_messages(
        payload=payload,
        request=request,
        authorization=authorization,
        x_api_key=x_api_key,
        db=db,
        settings=settings,
    )
