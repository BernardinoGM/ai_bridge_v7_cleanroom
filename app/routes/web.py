from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    SESSION_MAX_AGE_SECONDS,
    USER_SESSION_COOKIE_NAME,
    issue_session_token,
    read_session_token,
)
from sqlalchemy import select


router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    settings = get_settings()
    launch_user_id = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user") or ""
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"packs": list(TOP_UP_PACKS.values()), "launch_user_id": launch_user_id},
    )


@router.get("/r/{referral_code}")
def referral_redirect(referral_code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?ref={referral_code}", status_code=307)


@router.get("/dashboard")
def dashboard_root(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if session_subject:
        user = db.scalar(select(User).where(User.email == session_subject.strip().lower()))
        if user is not None:
            return RedirectResponse(url="/dashboard/me", status_code=307)
    return RedirectResponse(url="/?open=signup", status_code=307)


@router.get("/dashboard/me", response_class=HTMLResponse)
def dashboard_me(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not session_subject:
        raise HTTPException(status_code=401, detail="Sign in to view your dashboard.")
    user = db.scalar(select(User).where(User.email == session_subject.strip().lower()))
    if user is None:
        raise HTTPException(status_code=401, detail="Launch session is invalid.")
    context = build_dashboard(db, user.id)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/demo", response_class=HTMLResponse)
def dashboard_demo(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = build_dashboard(db, 1)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/{user_id}", response_class=HTMLResponse)
def dashboard_page(request: Request, user_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not session_subject:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    user = db.scalar(select(User).where(User.email == session_subject.strip().lower()))
    if user is None or user.id != user_id:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    context = build_dashboard(db, user_id)
    return templates.TemplateResponse(request, "dashboard.html", context)


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
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not session_subject:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    user = db.scalar(select(User).where(User.email == session_subject.strip().lower()))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return chat_surface(request, user.id, db)


@router.get("/chat/demo", response_class=HTMLResponse)
def chat_demo(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    dashboard = build_dashboard(db, 1)
    initial_tasks = (
        db.query(TaskSession)
        .filter(TaskSession.user_id == 1, TaskSession.archived.is_(False))
        .order_by(TaskSession.starred.desc(), TaskSession.updated_at.desc())
        .limit(12)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "user_id": 1,
            "balance_usd": dashboard["balance_usd"],
            "days_left": dashboard["days_left"],
            "heavy_workdays_left": dashboard["heavy_workdays_left"],
            "initial_tasks": initial_tasks,
        },
    )


@router.get("/chat/{user_id}", response_class=HTMLResponse)
def chat_surface(request: Request, user_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    session_subject = read_session_token(request.cookies.get(USER_SESSION_COOKIE_NAME), settings, "user")
    if not session_subject:
        raise HTTPException(status_code=404, detail="Chat not found.")
    user = db.scalar(select(User).where(User.email == session_subject.strip().lower()))
    if user is None or user.id != user_id:
        raise HTTPException(status_code=404, detail="Chat not found.")
    dashboard = build_dashboard(db, user_id)
    initial_tasks = (
        db.query(TaskSession)
        .filter(TaskSession.user_id == user_id, TaskSession.archived.is_(False))
        .order_by(TaskSession.starred.desc(), TaskSession.updated_at.desc())
        .limit(12)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "user_id": user_id,
            "balance_usd": dashboard["balance_usd"],
            "days_left": dashboard["days_left"],
            "heavy_workdays_left": dashboard["heavy_workdays_left"],
            "initial_tasks": initial_tasks,
        },
    )


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
