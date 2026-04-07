from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.dashboard import build_dashboard
from app.db import get_db
from app.pricing import TOP_UP_PACKS


router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"packs": list(TOP_UP_PACKS.values())},
    )


@router.get("/dashboard/{user_id}", response_class=HTMLResponse)
def dashboard_page(request: Request, user_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    context = build_dashboard(db, user_id)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/demo", response_class=HTMLResponse)
def dashboard_demo(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = build_dashboard(db, 1)
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/payments/success", response_class=HTMLResponse)
def payment_success(request: Request, session_id: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_success.html", {"session_id": session_id})


@router.get("/payments/cancel", response_class=HTMLResponse)
def payment_cancel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "payment_cancel.html", {})
