import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from . import models  # ensure models are registered before create_all
from .config import get_settings
from .database import Base, SessionLocal, engine
from .excel_parser import parse_name, parse_predictions, to_json
from .notifier import send_message
from .scheduler import seed_past_matches, start_scheduler, stop_scheduler
from .telegram_bot import polling_loop, send_predictions_to_user, notify_admin_new_user

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _migrate_db():
    """Add any new columns to existing tables without dropping data."""
    new_cols = {
        "notified_matches": {
            "home_team": "VARCHAR",
            "away_team": "VARCHAR",
            "home_score": "INTEGER",
            "away_score": "INTEGER",
            "duration": "VARCHAR",
            "prediction": "VARCHAR",
            "predicted_home_goals": "INTEGER",
            "predicted_away_goals": "INTEGER",
            "correct": "INTEGER",
            "points": "INTEGER",
        }
    }
    inspector = inspect(engine)
    with engine.connect() as conn:
        for table, cols in new_cols.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col, col_type in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    logger.info(f"Migration: added column {table}.{col}")
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    start_scheduler()
    settings = get_settings()
    bot_task = None
    if settings.telegram_bot_token:
        bot_task = asyncio.create_task(polling_loop(settings.telegram_bot_token))
    yield
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    stop_scheduler()


app = FastAPI(
    title="WC 2026 Prediction Notifier",
    description="Telegram notifications for your World Cup 2026 match predictions",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


async def _process_registration(telegram_chat_id: str, file_bytes: bytes):
    settings = get_settings()

    try:
        predictions = parse_predictions(file_bytes)
        name = parse_name(file_bytes) or "Unknown"
    except Exception as e:
        logger.error(f"Excel parse error for {telegram_chat_id}: {e}")
        if settings.telegram_bot_token:
            await send_message(settings.telegram_bot_token, telegram_chat_id,
                               f"❌ Could not parse your Excel file.\n{e}\n\nPlease check the file and try again.")
        return

    if not predictions:
        if settings.telegram_bot_token:
            await send_message(settings.telegram_bot_token, telegram_chat_id,
                               "❌ No predictions found in your Excel file. Make sure you're uploading the right file.")
        return

    predictions_json = to_json(predictions)

    db = SessionLocal()
    try:
        user = db.query(models.User).filter_by(telegram_chat_id=telegram_chat_id).first()
        is_new = user is None
        if user:
            user.name = name
            user.predictions = predictions_json
        else:
            user = models.User(name=name, telegram_chat_id=telegram_chat_id, predictions=predictions_json)
            db.add(user)
        db.commit()
    finally:
        db.close()

    if is_new:
        await seed_past_matches(telegram_chat_id)
    if settings.telegram_bot_token:
        await send_predictions_to_user(telegram_chat_id, settings.telegram_bot_token)
        if settings.admin_telegram_chat_id:
            await notify_admin_new_user(settings.admin_telegram_chat_id, settings.telegram_bot_token, name, telegram_chat_id, is_new)


@app.post("/register")
async def register(
    telegram_chat_id: str = Form(...),
    excel_file: UploadFile = File(...),
):
    if not (excel_file.filename or "").endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Please upload an Excel file (.xlsx or .xls)")

    file_bytes = await excel_file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")

    telegram_chat_id = telegram_chat_id.strip()
    if not telegram_chat_id.lstrip("-").isdigit():
        raise HTTPException(400, "Invalid Telegram chat ID — must be a numeric ID.")

    asyncio.create_task(_process_registration(telegram_chat_id, file_bytes))
    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health():
    return {"status": "ok"}
