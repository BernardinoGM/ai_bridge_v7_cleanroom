from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.config import get_settings
from app.dashboard import build_admin_dashboard, build_dashboard
from app.db import get_db
from app.models import TaskSession, User
from app.pricing import TOP_UP_PACKS
from app.session_auth import (
    ADMIN_SESSION_COOKIE_NAME,
    ADMIN_SESSION_MAX_AGE_SECONDS,
    SETUP_SESSION_COOKIE_NAME,
    USER_SESSION_COOKIE_NAME,
    issue_session_token,
    read_session_token,
)
from sqlalchemy import select


router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
BLOCKED_CHECKOUT_EMAILS = {"founder@aibridge.local", "bernard.gmny@gmail.com"}


def _current_user(request: Request, db: Session) -> User | None:
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not session_subject:
        return None
    return db.scalar(select(User).where(User.email == session_subject.strip().lower()))


def _render_dashboard(request: Request, db: Session, user: User) -> HTMLResponse:
    settings = get_settings()
    raw_key = read_session_token(request.cookies.get(SETUP_SESSION_COOKIE_NAME), settings, "setup")
    context = build_dashboard(db, user.id, raw_key=raw_key)
    return templates.TemplateResponse(request, "dashboard.html", context)


def _render_chat(request: Request, db: Session, user: User) -> HTMLResponse:
    dashboard = build_dashboard(db, user.id)
    initial_tasks = (
        db.query(TaskSession)
        .filter(TaskSession.user_id == user.id, TaskSession.archived.is_(False))
        .order_by(TaskSession.starred.desc(), TaskSession.updated_at.desc())
        .limit(12)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "user_id": user.id,
            "balance_usd": dashboard["balance_usd"],
            "days_left": dashboard["days_left"],
            "heavy_workdays_left": dashboard["heavy_workdays_left"],
            "initial_tasks": initial_tasks,
        },
    )


def _checkout_enabled_for_request(request: Request, db: Session) -> bool:
    settings = get_settings()
    if read_session_token(request.cookies.get(ADMIN_SESSION_COOKIE_NAME), settings, "admin"):
        return False
    user = _current_user(request, db)
    return bool(user and user.email.strip().lower() not in BLOCKED_CHECKOUT_EMAILS)


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = _current_user(request, db)
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "packs": list(TOP_UP_PACKS.values()),
            "launch_session_active": bool(current_user),
            "checkout_enabled": _checkout_enabled_for_request(request, db),
        },
    )


@router.get("/r/{referral_code}")
def referral_redirect(referral_code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?ref={referral_code}", status_code=307)


@router.get("/signup")
def signup_redirect(ref: str | None = None) -> RedirectResponse:
    target = "/?open=signup"
    if ref:
        target = f"{target}&ref={ref}"
    return RedirectResponse(url=target, status_code=307)


@router.get("/dashboard")
def dashboard_root(request: Request, db: Session = Depends(get_db)) -> Response:
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse(url="/?open=signup", status_code=307)
    return _render_dashboard(request, db, user)


@router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    lookup: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    admin_session = read_session_token(request.cookies.get(ADMIN_SESSION_COOKIE_NAME), settings, "admin")
    user_session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    bernard_authorized = False
    if user_session_subject:
        admin_user = db.scalar(select(User).where(User.email == user_session_subject.strip().lower()))
        bernard_authorized = bool(
            admin_user
            and (
                admin_user.email.strip().lower() == "bernard.gmny@gmail.com"
                or admin_user.name.strip().lower() == "bernard"
            )
        )
    if admin_session != "owner":
        if x_admin_key != settings.admin_api_key and not bernard_authorized:
            raise HTTPException(status_code=403, detail="Admin access required.")
    context = build_admin_dashboard(db)
    if lookup:
        normalized_lookup = f"%{lookup.strip().lower()}%"
        context["lookup_query"] = lookup
        context["lookup_users"] = db.query(User).filter(
            (User.email.ilike(normalized_lookup)) | (User.name.ilike(normalized_lookup))
        ).limit(20).all()
    else:
        context["lookup_query"] = ""
        context["lookup_users"] = []
    response = templates.TemplateResponse(request, "admin_dashboard.html", context)
    if admin_session != "owner":
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE_NAME,
            value=issue_session_token("owner", "admin", settings, ADMIN_SESSION_MAX_AGE_SECONDS),
            max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="strict",
            secure=settings.app_env == "production",
        )
    return response


@router.get("/chat", response_class=HTMLResponse)
def chat_root(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = _current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return _render_chat(request, db, user)


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "privacy.html", {})


@router.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "terms.html", {})


@router.get("/acceptable-use", response_class=HTMLResponse)
def acceptable_use_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "acceptable_use.html", {})


@router.get("/payments/success", response_class=HTMLResponse)
def payment_success(request: Request, session_id: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_success.html", {"session_id": session_id})


@router.get("/payments/cancel", response_class=HTMLResponse)
def payment_cancel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_cancel.html", {})
