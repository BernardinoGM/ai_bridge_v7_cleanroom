from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.config import get_settings
from app.dashboard import build_admin_dashboard, build_dashboard
from app.db import get_db
from app.models import TaskSession, User
from app.pricing import TOP_UP_PACKS


router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    launch_user_id = request.cookies.get("ab_launch_user", "")
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
    raw_user_id = request.cookies.get("ab_launch_user")
    if raw_user_id:
        try:
            user_id = int(raw_user_id)
        except ValueError:
            user_id = None
        if user_id is not None and db.get(User, user_id) is not None:
            return RedirectResponse(url=f"/dashboard/{user_id}", status_code=307)
    return RedirectResponse(url="/dashboard/demo", status_code=307)


@router.get("/dashboard/demo", response_class=HTMLResponse)
def dashboard_demo(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = build_dashboard(db, 1)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/{user_id}", response_class=HTMLResponse)
def dashboard_page(request: Request, user_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    context = build_dashboard(db, user_id)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, key: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    if key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Admin access required.")
    context = build_admin_dashboard(db)
    return templates.TemplateResponse(request, "admin_dashboard.html", context)


@router.get("/chat/{user_id}", response_class=HTMLResponse)
def chat_surface(request: Request, user_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
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


@router.get("/chat/demo", response_class=HTMLResponse)
def chat_demo(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return chat_surface(request, 1, db)


@router.get("/payments/success", response_class=HTMLResponse)
def payment_success(request: Request, session_id: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_success.html", {"session_id": session_id})


@router.get("/payments/cancel", response_class=HTMLResponse)
def payment_cancel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_cancel.html", {})
