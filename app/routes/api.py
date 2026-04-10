import logging
import re
import uuid

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.benchmark import benchmark_cost_usd
from app.billing import debit_usage, wallet_balance
from app.config import Settings, get_settings
from app.costing import estimate_serving_cost_usd
from app.dashboard import build_dashboard
from app.db import get_db, init_database
from app.agents import (
    ExecutionStrategy,
    RequestAssessment,
    assess_request,
    build_execution_strategy,
    get_or_create_agent_profile,
    hydrate_profile_for_request,
    runtime_plan_for_strategy,
    strategy_summary,
    update_profile_after_turn,
)
from app.api_keys import authenticate_api_key, issue_api_key, attach_referrer_by_code
from app.models import AgentProfile, DemoTrial, RequestFailure, TaskSession, TaskTurn, TrialSubsidy, UsageEvent, User
from app.payments import create_checkout_session, process_checkout_completed
from app.pricing import TOP_UP_PACKS, estimate_public_charge
from app.providers.base import ProviderClient, ProviderResponse
from app.providers.real import ProviderExecutionError, build_provider_clients
from app.providers.mock import build_mock_clients
from app.schemas import ApiKeyCreateRequest, ChatCompletionRequest, CheckoutCreateRequest, DemoChatRequest, MessagesRequest
from app.session_auth import (
    ADMIN_SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    SETUP_SESSION_COOKIE_NAME,
    SETUP_SESSION_MAX_AGE_SECONDS,
    USER_SESSION_COOKIE_NAME,
    issue_session_token,
    read_session_token,
)
from app.terminal import build_terminal_setup_commands
from app.tasks import record_task_turn, resolve_task


router = APIRouter()
logger = logging.getLogger(__name__)

DEMO_COOKIE_NAME = "ab_demo_session"
DEMO_TRIAL_LIMIT = 3
DEMO_TEMPORARY_MESSAGE = "AI Bridge preview is temporarily unavailable. Please try again in a moment."
DEMO_SYSTEM_PROMPT = (
    "You are AB, the public AI Bridge preview. "
    "Always reply in concise, natural English for a US developer audience. "
    "Keep the response short, direct, and helpful. "
    "Do not mention providers, routing, or internal model choices."
)
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


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

MODEL_ALIAS_TO_MODE = {
    "claude-sonnet-4-6": "assured",
    "claude-sonnet-4-5": "assured",
    "claude-3-7-sonnet-latest": "assured",
    "claude-opus-4-1": "assured",
}
COMPAT_TEMPORARY_MESSAGE = "This workflow is temporarily unavailable. Please retry in a moment."

BLOCKED_CHECKOUT_EMAILS = {"founder@aibridge.local", "bernard.gmny@gmail.com"}


def _task_status_label(task: TaskSession) -> str:
    if task.last_status_label:
        return task.last_status_label
    if task.pinned_lane == "assured":
        return "Verified"
    return "Checked" if task.quality_check_enabled else "In progress"


def _normalize_requested_mode(mode: str, model: str | None) -> str:
    if model:
        normalized = MODEL_ALIAS_TO_MODE.get(model.strip().lower())
        if normalized:
            return normalized
    return mode


def _apply_compat_alias(payload: ChatCompletionRequest | MessagesRequest) -> str | None:
    incoming_model = payload.model.strip().lower() if payload.model else None
    payload.mode = _normalize_requested_mode(payload.mode, payload.model)
    if incoming_model in MODEL_ALIAS_TO_MODE:
        payload.model = None
    return incoming_model


def _raise_neutral_compat_error(exc: HTTPException) -> None:
    detail = exc.detail if isinstance(exc.detail, str) else ""
    lowered = detail.lower()
    if any(marker in lowered for marker in ("selected model", "does not exist", "claude-", "anthropic")):
        raise HTTPException(status_code=503, detail=COMPAT_TEMPORARY_MESSAGE) from exc
    raise exc


def _compat_context(request: Request, observed_model: str | None, rewritten_mode: str) -> dict[str, str]:
    return {
        "request_path": str(request.url.path),
        "incoming_model": observed_model or "",
        "rewritten_mode": rewritten_mode,
        "provider_model": "",
    }


def _dispatch_compat_request(
    *,
    kind: str,
    payload: ChatCompletionRequest | MessagesRequest,
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    db: Session,
    settings: Settings,
) -> dict:
    observed_model = _apply_compat_alias(payload)
    context_json = _compat_context(request, observed_model, payload.mode)
    try:
        if kind == "chat_completions":
            return api_chat_completions(
                payload=payload,
                request=request,
                authorization=authorization,
                x_api_key=x_api_key,
                db=db,
                settings=settings,
            )
        return api_messages(
            payload=payload,
            request=request,
            authorization=authorization,
            x_api_key=x_api_key,
            db=db,
            settings=settings,
        )
    except HTTPException as exc:
        _record_failure(
            db,
            str(request.url.path),
            exc.detail if isinstance(exc.detail, str) else "compat failure",
            context_json=context_json,
        )
        _raise_neutral_compat_error(exc)
    except Exception:
        _record_failure(
            db,
            str(request.url.path),
            "compat failure",
            context_json=context_json,
        )
        raise HTTPException(status_code=503, detail=COMPAT_TEMPORARY_MESSAGE)


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


def _format_usd(amount: float) -> str:
    return f"${amount:.2f}"


def _build_strategy_for_prompt(
    *,
    prompt: str,
    profile: AgentProfile | None,
    surface: str,
    session_context: dict | None = None,
    workspace_context: dict | None = None,
) -> tuple[RequestAssessment, ExecutionStrategy]:
    assessment = assess_request(
        messages=[{"role": "user", "content": prompt}],
        user_profile=profile,
        surface=surface,
        session_context=session_context,
        workspace_context=workspace_context,
    )
    strategy = build_execution_strategy(
        assessment,
        profile,
        session_context=session_context,
        workspace_context=workspace_context,
    )
    return assessment, strategy


def _sanitize_ab_reply(prompt: str, reply: str, strategy: ExecutionStrategy) -> str:
    normalized_prompt = prompt.strip().lower()
    lowered = reply.lower()
    blocked_markers = (
        "selected model",
        "does not exist",
        "may not have access",
        "handled in the",
        "fast lane",
        "smart lane",
        "assured lane",
        "premium reasoning",
        "quality checks are applied",
        "provider",
        "deepseek",
        "anthropic",
        "claude",
    )
    blocked = any(marker in lowered for marker in blocked_markers)
    if not _CJK_PATTERN.search(reply) and not blocked:
        return reply
    if strategy.user_visible_mode == "preview":
        if normalized_prompt in {"hello", "hi", "hey", "hello!", "hi!", "hey!"} or len(normalized_prompt) <= 80:
            return "Hi, I’m AB. Paste a bug, repo task, or code question to get started."
        return "I’m AB. Share the bug, repo task, or code question and I’ll help you work through it."
    if normalized_prompt in {"hello", "hi", "hey", "hello!", "hi!", "hey!"} or len(normalized_prompt) <= 80:
        return "Hello! How can I help you today?"
    if blocked or _CJK_PATTERN.search(reply):
        return "I can help with that. Share the repo task, error, or next step."
    return reply


def _get_or_create_demo_trial(db: Session, session_id: str) -> DemoTrial:
    trial = db.scalar(select(DemoTrial).where(DemoTrial.session_id == session_id))
    if trial:
        return trial
    trial = DemoTrial(session_id=session_id, tries_used=0)
    db.add(trial)
    db.flush()
    return trial


def _get_or_create_demo_trial_resilient(db: Session, session_id: str) -> DemoTrial:
    try:
        return _get_or_create_demo_trial(db, session_id)
    except SQLAlchemyError:
        logger.exception("Demo trial lookup failed; reinitializing runtime tables")
        db.rollback()
        init_database()
        return _get_or_create_demo_trial(db, session_id)


def _persist_demo_preview(
    *,
    db: Session,
    trial: DemoTrial,
    session_id: str,
    example_key: str,
    prompt: str,
    preview: dict,
    settings: Settings,
) -> dict:
    routed_cost_usd = float(preview["routed_cost"].replace("$", ""))
    direct_cost_usd = float(preview["direct_cost"].replace("$", ""))
    estimated_prompt_tokens = 160
    estimated_completion_tokens = 220
    serving_cost = estimate_serving_cost_usd(
        provider_key=preview["execution_profile"],
        prompt_tokens=estimated_prompt_tokens,
        completion_tokens=estimated_completion_tokens,
        public_charge_usd=routed_cost_usd,
        quality_check=preview["quality"] in {"Checked", "Verified"},
        fallback_used=preview["fallback_used"],
        retry_count=0,
    )
    benchmark = benchmark_cost_usd(estimated_prompt_tokens, estimated_completion_tokens, settings)
    trial.tries_used += 1
    trial.last_example = example_key
    db.add(
        TrialSubsidy(
            demo_trial_id=trial.id,
            session_id=session_id,
            request_id=uuid.uuid4().hex,
            prompt_excerpt=prompt[:255],
            execution_profile=preview["execution_profile"],
            visible_lane=(preview["lane"] or "Smart").lower(),
            direct_cost_usd=direct_cost_usd,
            routed_cost_usd=routed_cost_usd,
            serving_cogs_usd=serving_cost.serving_cogs_usd,
            benchmark_cost_usd=benchmark,
            saved_pct=int(preview["saved_pct"]),
        )
    )
    db.commit()
    reason = "Preview completed in the fast AB preview flow."
    preview["reason"] = reason
    preview["why"] = reason
    preview["trial_remaining"] = max(0, DEMO_TRIAL_LIMIT - trial.tries_used)
    preview["tries_remaining"] = preview["trial_remaining"]
    preview["trial_exhausted"] = trial.tries_used >= DEMO_TRIAL_LIMIT
    preview["show_signup_after_ms"] = 7000 if preview["trial_exhausted"] else 0
    return preview


def _public_demo_response(preview: dict) -> dict:
    return {
        "reply": preview["reply"],
        "trial_remaining": preview["trial_remaining"],
        "tries_remaining": preview["tries_remaining"],
        "trial_exhausted": preview["trial_exhausted"],
        "show_signup_after_ms": preview["show_signup_after_ms"],
    }


def _execute_with_fallback(
    execution_profile: str,
    fallback_execution_profile: str | None,
    registry: dict[str, ProviderClient],
    prompt: str,
    system: str | None,
) -> tuple[ProviderResponse, str, bool]:
    if execution_profile not in registry:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please try again.")
    try:
        return registry[execution_profile].generate(prompt=prompt, system=system), execution_profile, False
    except ProviderExecutionError:
        if not fallback_execution_profile or fallback_execution_profile not in registry:
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please try again.")
        try:
            fallback_response = registry[fallback_execution_profile].generate(prompt=prompt, system=system)
            return fallback_response, fallback_execution_profile, True
        except ProviderExecutionError as exc:
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please try again.") from exc


def _execute_demo_preview(
    *,
    prompt: str,
    settings: Settings,
    strategy: ExecutionStrategy,
    system: str | None = None,
) -> tuple[ExecutionStrategy, ProviderResponse, str, bool]:
    runtime_plan = runtime_plan_for_strategy(strategy, "try")
    registry = _provider_registry(settings)
    try:
        if runtime_plan.primary_execution_profile not in registry:
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please try again.")
        provider_response = registry[runtime_plan.primary_execution_profile].generate(prompt=prompt, system=system)
        return strategy, provider_response, runtime_plan.primary_execution_profile, False
    except (HTTPException, ProviderExecutionError):
        provider_response = ProviderResponse(
            text="Hi, I’m AB. Paste a bug, repo task, or code question to get started.",
            latency_ms=0,
            prompt_tokens_est=max(24, len(prompt) // 4),
            completion_tokens_est=96,
            retry_count=0,
            fallback_used=True,
        )
        return strategy, provider_response, "preview_fallback", True


def _route_preview(
    *,
    prompt: str,
    settings: Settings,
    strategy: ExecutionStrategy,
    system: str | None = None,
) -> dict:
    strategy, provider_response, execution_profile_used, fallback_used = _execute_demo_preview(
        prompt=prompt,
        settings=settings,
        strategy=strategy,
        system=system,
    )
    runtime_plan = runtime_plan_for_strategy(strategy, "try")
    visible_lane = runtime_plan.visible_mode
    routed_cost = estimate_public_charge(
        mode=visible_lane,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=runtime_plan.quality_check,
    )
    direct_premium_cost = estimate_public_charge(
        mode="assured",
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=True,
    )
    saved_pct = max(0, round((1 - (routed_cost / max(direct_premium_cost, 0.01))) * 100))
    return {
        "reply": _sanitize_ab_reply(prompt, provider_response.text, strategy),
        "lane": runtime_plan.visible_mode.title(),
        "quality": runtime_plan.status_label,
        "direct_cost": _format_usd(direct_premium_cost),
        "routed_cost": _format_usd(routed_cost),
        "saved_pct": saved_pct,
        "why": "Preview completed in the fast AB preview flow.",
        "execution_profile": execution_profile_used,
        "fallback_used": fallback_used,
        "premium_escalated": runtime_plan.premium_escalated,
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


def _cookie_user(request: Request, db: Session) -> User | None:
    settings = get_settings()
    raw = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not raw:
        return None
    return db.scalar(select(User).where(User.email == raw.strip().lower()))


def _trusted_checkout_user(request: Request, db: Session, settings: Settings) -> User | None:
    if read_session_token(request.cookies.get(ADMIN_SESSION_COOKIE_NAME), settings, "admin"):
        return None
    cookie_user = _cookie_user(request, db)
    if cookie_user is None:
        return None
    if cookie_user.email.strip().lower() in BLOCKED_CHECKOUT_EMAILS:
        return None
    setup_key = read_session_token(request.cookies.get(SETUP_SESSION_COOKIE_NAME), settings, "setup")
    if not setup_key:
        return None
    setup_user = authenticate_api_key(db, settings, setup_key)
    if setup_user is None or setup_user.id != cookie_user.id:
        return None
    return cookie_user


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
    cookie_user = _cookie_user(request, db)
    if cookie_user is not None:
        return cookie_user.id
    raise HTTPException(status_code=401, detail="Authentication required. Provide a valid API key or launch session.")


@router.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "vpn_required": False,
    }


@router.post("/demo/chat")
def demo_chat(
    payload: DemoChatRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    session_id = request.cookies.get(DEMO_COOKIE_NAME) or uuid.uuid4().hex
    example_key = payload.example or "spec"
    prompt = (payload.message or "").strip()
    if not prompt:
        prompt = DEMO_EXAMPLES[example_key]["prompt"]
    try:
        trial = _get_or_create_demo_trial_resilient(db, session_id)
        if trial.tries_used >= DEMO_TRIAL_LIMIT:
            raise HTTPException(status_code=429, detail="Anonymous demo limit reached. Open the dashboard demo or request API access to continue.")
        _, strategy = _build_strategy_for_prompt(
            prompt=prompt[:8000],
            profile=None,
            surface="try",
            session_context={"session_id": session_id, "tries_used": trial.tries_used},
        )
        preview = _route_preview(prompt=prompt[:8000], settings=settings, strategy=strategy, system=DEMO_SYSTEM_PROMPT)
        try:
            preview = _persist_demo_preview(
                db=db,
                trial=trial,
                session_id=session_id,
                example_key=example_key,
                prompt=prompt,
                preview=preview,
                settings=settings,
            )
        except SQLAlchemyError:
            logger.exception("Demo preview persistence failed; reinitializing runtime tables")
            db.rollback()
            init_database()
            trial = _get_or_create_demo_trial_resilient(db, session_id)
            if trial.tries_used >= DEMO_TRIAL_LIMIT:
                raise HTTPException(status_code=429, detail="Anonymous demo limit reached. Open the dashboard demo or request API access to continue.")
            preview = _persist_demo_preview(
                db=db,
                trial=trial,
                session_id=session_id,
                example_key=example_key,
                prompt=prompt,
                preview=preview,
                settings=settings,
            )
        response.set_cookie(
            key=DEMO_COOKIE_NAME,
            value=session_id,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
        )
        return _public_demo_response(preview)
    except HTTPException as exc:
        _record_failure(db, "/demo/chat", exc.detail if isinstance(exc.detail, str) else "demo failure", context_json={"example": example_key})
        raise
    except Exception as exc:
        logger.exception("Demo preview failed")
        db.rollback()
        try:
            _record_failure(db, "/demo/chat", str(exc)[:255] or "demo failure", context_json={"example": example_key})
        except Exception:
            db.rollback()
        raise HTTPException(status_code=503, detail=DEMO_TEMPORARY_MESSAGE)


@router.post("/keys")
@router.post("/v1/keys")
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
            key=USER_SESSION_COOKIE_NAME,
            value=issue_session_token(user.email.strip().lower(), "user", settings, SESSION_MAX_AGE_SECONDS),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=settings.app_env == "production",
        )
        response.set_cookie(
            key=SETUP_SESSION_COOKIE_NAME,
            value=issue_session_token(raw_key, "setup", settings, SETUP_SESSION_MAX_AGE_SECONDS),
            max_age=SETUP_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=settings.app_env == "production",
        )
        response.delete_cookie(ADMIN_SESSION_COOKIE_NAME)
        return {
            "api_key": raw_key,
            "user_id": user.id,
            "email": user.email,
            "granted_credit_usd": round(granted_credit, 2),
            "balance_usd": round(balance_usd, 2),
            "dashboard_url": "/dashboard",
            "chat_url": "/chat",
            "onboarding_commands": build_terminal_setup_commands(raw_key, settings),
            "terminal_command": settings.terminal_cli_command,
        }
    except HTTPException as exc:
        _record_failure(db, "/v1/keys", exc.detail if isinstance(exc.detail, str) else "signup failed", context_json={"email": payload.email})
        raise


@router.get("/api/topups/packs")
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


@router.post("/api/payments/checkout")
def create_checkout(
    payload: CheckoutCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        cookie_user = _trusted_checkout_user(request, db, settings)
        if cookie_user is None:
            raise HTTPException(
                status_code=403,
                detail="Top-up temporarily unavailable during launch verification.",
            )
        current_user = cookie_user
        if payload.user_id is not None and payload.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Checkout can only be created for the current signed-in user.")
        if payload.email and payload.email.strip().lower() != current_user.email.lower():
            raise HTTPException(status_code=403, detail="Checkout email does not match the current signed-in user.")
        attach_referrer_by_code(db, current_user, payload.referred_by_code)
        resolved_user_id = current_user.id
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
    except Exception:
        _record_failure(db, "/api/payments/checkout", "checkout unavailable", context_json={"pack_code": payload.pack_code})
        raise HTTPException(status_code=503, detail="Checkout is temporarily unavailable. Please try again.")


@router.post("/api/payments/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(alias="Stripe-Signature"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if not settings.payment_ready:
        _record_failure(db, "/api/payments/webhook", "stripe not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook processing unavailable")
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
                session_metadata=session_data.get("metadata") or {},
            )
            db.commit()
            return JSONResponse({"processed": processed})
        except Exception as exc:
            _record_failure(db, "/api/payments/webhook", str(exc), context_json={"session_id": session_data.get("id")})
            raise
    return JSONResponse({"processed": False, "ignored": event["type"]})


@router.get("/api/tasks/{user_id}")
def list_tasks(user_id: int, request: Request, archived: bool = False, db: Session = Depends(get_db)) -> dict:
    cookie_user = _cookie_user(request, db)
    if cookie_user is None or cookie_user.id != user_id:
        raise HTTPException(status_code=404, detail="Task list not found.")
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


@router.get("/api/tasks/{user_id}/{task_id}")
def get_task_thread(user_id: int, task_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    cookie_user = _cookie_user(request, db)
    if cookie_user is None or cookie_user.id != user_id:
        raise HTTPException(status_code=404, detail="Task not found.")
    task = db.scalar(select(TaskSession).where(TaskSession.user_id == user_id, TaskSession.task_id == task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    turns = db.scalars(
        select(TaskTurn).where(TaskTurn.task_session_id == task.id).order_by(TaskTurn.created_at.asc(), TaskTurn.id.asc())
    ).all()
    return _serialize_task_thread(task, turns)


@router.get("/api/admin/usage/{request_id}")
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


@router.get("/api/admin/agents/{user_id}")
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


def _complete_terminal_chat(
    user_id: int,
    strategy: ExecutionStrategy,
    system: str | None,
    prompt: str,
    db: Session,
    settings: Settings,
    endpoint: str,
    task_id: str | None = None,
) -> dict:
    runtime_plan = runtime_plan_for_strategy(strategy, "terminal")
    registry = _provider_registry(settings)
    provider_response, execution_profile_used, fallback_used = _execute_with_fallback(
        runtime_plan.primary_execution_profile,
        runtime_plan.fallback_execution_profile,
        registry,
        prompt,
        system,
    )
    reply_text = _sanitize_ab_reply(prompt, provider_response.text, strategy)
    public_charge = estimate_public_charge(
        mode=runtime_plan.visible_mode,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=runtime_plan.quality_check,
    )
    if wallet_balance(db, user_id, "main") < public_charge:
        raise HTTPException(status_code=402, detail="Insufficient main balance. Top up to continue.")
    cost_estimate = estimate_serving_cost_usd(
        provider_key=execution_profile_used,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        public_charge_usd=public_charge,
        quality_check=runtime_plan.quality_check,
        fallback_used=fallback_used,
        retry_count=provider_response.retry_count,
    )
    benchmark = benchmark_cost_usd(provider_response.prompt_tokens_est, provider_response.completion_tokens_est, settings)
    request_id = uuid.uuid4().hex
    debit_usage(
        db,
        user_id=user_id,
        amount_usd=public_charge,
        request_id=request_id,
        mode=runtime_plan.visible_mode,
    )
    event = UsageEvent(
        user_id=user_id,
        task_id=task_id,
        request_id=request_id,
        endpoint=endpoint,
        mode=runtime_plan.visible_mode,
        public_charge_usd=public_charge,
        serving_cogs_usd=cost_estimate.serving_cogs_usd,
        benchmark_cost_usd=benchmark,
        gross_margin_guardrail_usd=cost_estimate.guardrail_usd,
        route_chosen=execution_profile_used,
        premium_escalated=runtime_plan.premium_escalated,
        local_model_hit=False,
        fallback_used=fallback_used,
        retry_count=provider_response.retry_count,
        quality_check_triggered=runtime_plan.quality_check,
        latency_ms=provider_response.latency_ms,
        prompt_tokens_est=provider_response.prompt_tokens_est,
        completion_tokens_est=provider_response.completion_tokens_est,
        request_payload={"prompt": prompt[:500], "mode": runtime_plan.visible_mode, "strategy": strategy_summary(strategy)},
        response_excerpt=reply_text[:200],
    )
    db.add(event)
    db.commit()
    return {
        "id": f"ab_{request_id}",
        "content": reply_text,
        "mode": runtime_plan.visible_mode,
        "request_id": request_id,
        "provider_family": execution_profile_used,
        "fallback_used": fallback_used,
        "strategy": strategy_summary(strategy),
        "usage": {
            "prompt_tokens": provider_response.prompt_tokens_est,
            "completion_tokens": provider_response.completion_tokens_est,
        },
        "billing": {
            "public_charge_usd": public_charge,
            "balance_remaining_usd": wallet_balance(db, user_id, "main"),
        },
        "telemetry": {"premium_escalated": runtime_plan.premium_escalated, "quality_check_triggered": runtime_plan.quality_check, "local_model_hit": False},
    }


@router.post("/api/chat/completions")
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
    source_surface = "terminal_compat" if request.url.path.startswith("/v1/") else "terminal_api"
    hydrate_profile_for_request(profile, prompt, source_surface)
    _, strategy = _build_strategy_for_prompt(
        prompt=prompt,
        profile=profile,
        surface="terminal",
        session_context={"surface": source_surface},
    )
    result = _complete_terminal_chat(
        user_id,
        strategy,
        system,
        prompt,
        db,
        settings,
        str(request.url.path),
    )
    update_profile_after_turn(
        profile=profile,
        turn_input={"task_id": result["request_id"], "prompt": prompt, "surface": source_surface},
        turn_output={"reply": result["content"]},
        strategy=strategy,
        observed_signals={
            "provider_family": result["provider_family"],
            "execution_profile": result["provider_family"],
            "premium_escalated": result["telemetry"]["premium_escalated"],
            "fallback_used": result["fallback_used"],
            "task_stable": not result["fallback_used"] and not result["telemetry"]["premium_escalated"],
            "ds_succeeded_cleanly": result["provider_family"] != "premium_anthropic" and not result["fallback_used"],
            "status_label": "Checked" if result["telemetry"]["quality_check_triggered"] else "In progress",
        },
    )
    db.commit()
    return {
        "id": result["id"],
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]}, "finish_reason": "stop"}],
        "usage": result["usage"],
        "ab": {"mode": result["mode"].title(), "status": "Checked" if result["telemetry"]["quality_check_triggered"] else "In progress", "billing": result["billing"]},
    }


@router.post("/terminal/messages")
@router.post("/api/messages")
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
    profile = get_or_create_agent_profile(db, user_id)
    source_surface = payload.source_surface or ("terminal_compat" if request.url.path.startswith("/v1/") else "terminal_api")
    hydrate_profile_for_request(profile, prompt, source_surface)
    _, strategy = _build_strategy_for_prompt(
        prompt=prompt,
        profile=profile,
        surface="terminal",
        session_context={"task_id": payload.task_id, "task_action": payload.task_action, "surface": source_surface},
    )
    task = resolve_task(
        db,
        user_id,
        _normalize_requested_mode(payload.mode, payload.model),
        prompt,
        payload.task_id,
        payload.task_action,
        source_surface=source_surface,
    )
    runtime_plan = runtime_plan_for_strategy(strategy, "terminal")
    if not (task.pinned_lane == "assured" and payload.task_action != "deescalate"):
        task.pinned_lane = runtime_plan.visible_mode
        task.internal_lane = strategy.primary_lane
        task.pinned_provider = "ab_orchestrator"
        task.pinned_execution_profile = runtime_plan.primary_execution_profile
        task.quality_check_enabled = runtime_plan.quality_check
        task.last_status_label = runtime_plan.status_label
    notes = dict(task.notes_json or {})
    notes["strategy"] = strategy_summary(strategy)
    task.notes_json = notes
    result = _complete_terminal_chat(
        user_id,
        strategy,
        payload.system,
        prompt,
        db,
        settings,
        str(request.url.path),
        task_id=task.task_id,
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
        source_surface,
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


@router.post("/v1/chat/completions")
def v1_chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return _dispatch_compat_request(
        kind="chat_completions",
        payload=payload,
        request=request,
        authorization=authorization,
        x_api_key=x_api_key,
        db=db,
        settings=settings,
    )


@router.post("/v1/messages")
def v1_messages(
    payload: MessagesRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return _dispatch_compat_request(
        kind="messages",
        payload=payload,
        request=request,
        authorization=authorization,
        x_api_key=x_api_key,
        db=db,
        settings=settings,
    )
