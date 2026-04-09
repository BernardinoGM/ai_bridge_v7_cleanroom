from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, get_settings
from app.db import Base, engine, session_scope
from app.models import AddOnSubscription
from app.payments import ensure_seed_user
from app.routes.api import compat_router, demo_router, router as api_router
from app.routes.web import router as web_router
from app.add_ons import ADD_ONS


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
app.include_router(api_router)
app.include_router(compat_router)
app.include_router(demo_router)
app.include_router(web_router)


def bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    with session_scope() as db:
        user = ensure_seed_user(db, email="founder@aibridge.local", name="Founder", referral_code="FOUNDER10")
        ensure_seed_user(db, email="Bernard.gmny@gmail.com", name="Bernard", referral_code="BERNARD10")
        for addon in ADD_ONS:
            exists = (
                db.query(AddOnSubscription)
                .filter(AddOnSubscription.user_id == user.id, AddOnSubscription.addon_code == addon["code"])
                .first()
            )
            if not exists:
                db.add(
                    AddOnSubscription(
                        user_id=user.id,
                        addon_code=addon["code"],
                        status="available",
                        monthly_price_usd=addon["price_usd"],
                        metadata_json={"tagline": addon["tagline"]},
                    )
                )


@app.on_event("startup")
def on_startup() -> None:
    bootstrap()
