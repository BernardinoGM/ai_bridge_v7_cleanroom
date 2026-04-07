import uuid

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.benchmark import benchmark_cost_usd
from app.billing import debit_usage, wallet_balance
from app.config import Settings, get_settings
from app.costing import estimate_serving_cost_usd
from app.dashboard import build_dashboard
from app.db import get_db
from app.agents import choose_initial_lane, get_or_create_agent_profile, update_profile_after_turn
from app.models import AgentProfile, UsageEvent
from app.payments import create_checkout_session, process_checkout_completed
from app.pricing import TOP_UP_PACKS, estimate_public_charge
from app.providers.base import ProviderClient, ProviderResponse
from app.providers.real import ProviderExecutionError, build_provider_clients
from app.providers.mock import build_mock_clients
from app.routing import RouteDecision, decide_route
from app.schemas import ChatCompletionRequest, CheckoutCreateRequest, MessagesRequest
from app.tasks import record_task_turn, resolve_task


router = APIRouter(prefix="/api")
compat_router = APIRouter(prefix="/v1")


def _provider_registry(settings: Settings) -> dict[str, ProviderClient]:
    if settings.provider_mock_enabled or settings.app_env == "testing":
        return build_mock_clients()
    return build_provider_clients(settings)


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


def _require_admin_key(x_admin_key: str | None, settings: Settings) -> None:
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Admin access required.")


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "vpn_required": False,
    }


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
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    result = create_checkout_session(
        db=db,
        settings=settings,
        user_id=payload.user_id,
        pack_code=payload.pack_code,
        referred_by_code=payload.referred_by_code,
    )
    db.commit()
    return {"checkout_url": result.checkout_url, "session_id": result.session_id}


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature") from exc
    if event["type"] == "checkout.session.completed":
        session_data = event["data"]["object"]
        processed = process_checkout_completed(
            db=db,
            event_id=event["id"],
            stripe_session_id=session_data["id"],
            stripe_payment_intent_id=session_data.get("payment_intent"),
        )
        db.commit()
        return JSONResponse({"processed": processed})
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
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if payload.stream:
        raise HTTPException(status_code=400, detail="Streaming is disabled in launch mode for accurate billing.")
    system = next((message.content for message in payload.messages if message.role == "system"), None)
    prompt = "\n".join(message.content for message in payload.messages if message.role == "user")
    profile = get_or_create_agent_profile(db, payload.user_id)
    visible_lane, internal_lane, quality_check = choose_initial_lane(profile, payload.mode, prompt)
    result = _complete_chat(
        payload.user_id,
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
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if payload.stream:
        raise HTTPException(status_code=400, detail="Streaming is disabled in launch mode for accurate billing.")
    prompt = "\n".join(str(item.get("content", "")) for item in payload.messages if item.get("role") == "user")
    task = resolve_task(db, payload.user_id, payload.mode, prompt, payload.task_id, payload.task_action)
    result = _complete_chat(
        payload.user_id,
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
    }


@compat_router.post("/chat/completions")
def v1_chat_completions(
    payload: ChatCompletionRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return api_chat_completions(payload=payload, db=db, settings=settings)


@compat_router.post("/messages")
def v1_messages(
    payload: MessagesRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return api_messages(payload=payload, db=db, settings=settings)
