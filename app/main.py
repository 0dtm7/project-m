import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .db import get_conn

app = FastAPI(title="PROJECT M backend", version="0.3.2")

QTICKETS_WEBHOOK_SECRET = os.getenv("QTICKETS_WEBHOOK_SECRET", "")
QTICKETS_EVENT_ID = int(os.getenv("QTICKETS_EVENT_ID", "251223"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://project-m-a8gu.onrender.com"
).rstrip("/")

MINI_APP_URL = f"{PUBLIC_BASE_URL}/app"
TELEGRAM_WEBHOOK_URL = f"{PUBLIC_BASE_URL}/api/telegram/webhook"
QTICKETS_EVENT_URL = f"https://qtickets.ru/event/{QTICKETS_EVENT_ID}"

APP_HTML = Path(__file__).with_name("index.html")


class TelegramAuthBody(BaseModel):
    init_data: str


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "project-m-backend",
        "app": "/app",
        "version": "0.3.2",
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "project-m-backend", "version": "0.3.2"}


@app.get("/app")
def mini_app():
    return FileResponse(APP_HTML)


# ---------------------------
# Telegram Bot API
# ---------------------------

def telegram_api(method: str, payload: dict[str, Any] | None = None):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def project_m_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "ОТКРЫТЬ ПРОЕКТ «М»",
                    "web_app": {"url": MINI_APP_URL},
                }
            ]
        ]
    }


def send_project_m_start(chat_id: int):
    text = (
        "<b>ПРОЕКТ «М»</b>\n"
        "05.09 • 18:00 • КОМПРОМАТ\n\n"
        "Билеты, таймер, line-up и твой пропуск — внутри приложения.\n\n"
        "<b>До 22:00 — 16+</b>\n"
        "<b>После 22:00 — 18+</b>"
    )

    return telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": project_m_keyboard(),
        },
    )


@app.on_event("startup")
def configure_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot setup skipped: TELEGRAM_BOT_TOKEN missing")
        return

    try:
        telegram_api(
            "setWebhook",
            {
                "url": TELEGRAM_WEBHOOK_URL,
                "allowed_updates": ["message"],
            },
        )

        # Убираем список команд — бот используется как вход в Mini App.
        telegram_api("deleteMyCommands", {})

        print("Telegram webhook configured")
    except Exception as exc:
        print(f"Telegram setup failed: {type(exc).__name__}")


@app.post("/api/telegram/webhook")
async def telegram_bot_webhook(request: Request):
    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}

    if chat.get("type") != "private" or not chat.get("id"):
        return {"ok": True}

    text = (message.get("text") or "").strip()
    chat_id = int(chat["id"])

    # /start — красивое приветствие.
    if text.startswith("/start"):
        try:
            send_project_m_start(chat_id)
        except Exception:
            pass
        return {"ok": True}

    # Если человек всё же пишет в поле ввода —
    # мягко возвращаем его в приложение.
    if text:
        try:
            telegram_api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Всё основное — внутри ПРОЕКТА «М» ↓",
                    "reply_markup": project_m_keyboard(),
                },
            )
        except Exception:
            pass

    return {"ok": True}


# ---------------------------
# QTickets
# ---------------------------

def verify_qtickets_signature(body: bytes, signature: str | None) -> None:
    if not QTICKETS_WEBHOOK_SECRET:
        raise HTTPException(500, "QTICKETS_WEBHOOK_SECRET is not configured")
    if not signature:
        raise HTTPException(401, "Missing X-Signature")

    expected = hmac.new(
        QTICKETS_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha1,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid X-Signature")


# ---------------------------
# Telegram Mini App auth
# ---------------------------

def verify_telegram_init_data(init_data: str) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(500, "TELEGRAM_BOT_TOKEN is not configured")
    if not init_data:
        raise HTTPException(401, "Missing Telegram initData")

    items = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = items.pop("hash", None)

    if not received_hash:
        raise HTTPException(401, "Telegram hash is missing")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(items.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "Invalid Telegram initData")

    try:
        auth_date = int(items.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Invalid auth_date")

    now = int(time.time())
    if auth_date <= 0 or auth_date > now + 300 or now - auth_date > 86400:
        raise HTTPException(401, "Telegram initData expired")

    try:
        user = json.loads(items.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(401, "Invalid Telegram user")

    if not user.get("id"):
        raise HTTPException(401, "Telegram user is missing")

    return user


def get_telegram_user_from_header(init_data: str | None) -> dict[str, Any]:
    return verify_telegram_init_data(init_data or "")


def upsert_telegram_user(cur, user: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO telegram_users (
            telegram_id, username, first_name, last_name,
            photo_url, language_code, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            photo_url = EXCLUDED.photo_url,
            language_code = EXCLUDED.language_code,
            updated_at = NOW()
        """,
        (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
            user.get("photo_url"),
            user.get("language_code"),
        ),
    )


def normalize_custom(value: Any):
    if value in (None, ""):
        return None

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value

    return value


def extract_purchase_token(value: Any) -> str | None:
    value = normalize_custom(value)

    if isinstance(value, dict):
        token = value.get("purchase_token")
        return str(token) if token else None

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("purchase_token"):
                return str(item["purchase_token"])

    return None


def client_fields(payload: dict[str, Any]):
    client = payload.get("client") or {}
    details = client.get("details") or {}
    email = client.get("email")
    phone = details.get("phone")
    name = " ".join(
        x for x in [details.get("name"), details.get("surname")] if x
    ) or None
    return email, phone, name


def save_event(cur, event_type: str, payload: dict[str, Any]):
    cur.execute(
        """
        INSERT INTO qt_webhook_events(event_type, order_id, payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (event_type, payload.get("id"), json.dumps(payload, ensure_ascii=False)),
    )


def resolve_telegram_id(cur, purchase_token: str | None) -> int | None:
    if not purchase_token:
        return None

    cur.execute(
        """
        SELECT telegram_id
        FROM purchase_sessions
        WHERE token=%s AND expires_at > NOW()
        """,
        (purchase_token,),
    )
    row = cur.fetchone()
    return row["telegram_id"] if row else None


def upsert_order(cur, payload: dict[str, Any], status: str):
    email, phone, name = client_fields(payload)
    custom = normalize_custom(payload.get("custom"))
    purchase_token = extract_purchase_token(custom)
    telegram_id = resolve_telegram_id(cur, purchase_token)

    cur.execute(
        """
        INSERT INTO qt_orders (
            id, uniqid, event_id, status, payed, payed_at, price,
            original_price, currency_id, client_email, client_phone,
            client_name, custom, purchase_token, telegram_id, raw, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s::jsonb, %s, %s, %s::jsonb, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            uniqid = EXCLUDED.uniqid,
            event_id = EXCLUDED.event_id,
            status = EXCLUDED.status,
            payed = EXCLUDED.payed,
            payed_at = EXCLUDED.payed_at,
            price = EXCLUDED.price,
            original_price = EXCLUDED.original_price,
            currency_id = EXCLUDED.currency_id,
            client_email = EXCLUDED.client_email,
            client_phone = EXCLUDED.client_phone,
            client_name = EXCLUDED.client_name,
            custom = EXCLUDED.custom,
            purchase_token = COALESCE(EXCLUDED.purchase_token, qt_orders.purchase_token),
            telegram_id = COALESCE(EXCLUDED.telegram_id, qt_orders.telegram_id),
            raw = EXCLUDED.raw,
            updated_at = NOW()
        """,
        (
            payload["id"],
            payload.get("uniqid"),
            payload.get("event_id"),
            status,
            payload.get("payed"),
            payload.get("payed_at"),
            payload.get("price"),
            payload.get("original_price"),
            payload.get("currency_id"),
            email,
            phone,
            name,
            json.dumps(custom, ensure_ascii=False),
            purchase_token,
            telegram_id,
            json.dumps(payload, ensure_ascii=False),
        ),
    )

    if purchase_token and telegram_id:
        cur.execute(
            """
            UPDATE purchase_sessions
            SET status=%s, qt_order_id=%s
            WHERE token=%s
            """,
            ("paid" if payload.get("payed") else "linked", payload["id"], purchase_token),
        )


def upsert_ticket(cur, order_id: int, basket: dict[str, Any], status: str = "active"):
    cur.execute(
        """
        INSERT INTO qt_tickets (
            id, order_id, barcode, show_id, price,
            client_email, client_phone, client_name,
            status, raw, updated_at,
            ticket_name, seat_id, original_price, discount_value,
            pdf_url, passbook_url, series
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s::jsonb, NOW(),
            %s, %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (id) DO UPDATE SET
            barcode = EXCLUDED.barcode,
            show_id = EXCLUDED.show_id,
            price = EXCLUDED.price,
            client_email = EXCLUDED.client_email,
            client_phone = EXCLUDED.client_phone,
            client_name = EXCLUDED.client_name,
            status = EXCLUDED.status,
            raw = EXCLUDED.raw,
            ticket_name = EXCLUDED.ticket_name,
            seat_id = EXCLUDED.seat_id,
            original_price = EXCLUDED.original_price,
            discount_value = EXCLUDED.discount_value,
            pdf_url = EXCLUDED.pdf_url,
            passbook_url = EXCLUDED.passbook_url,
            series = EXCLUDED.series,
            updated_at = NOW()
        """,
        (
            basket["id"],
            order_id,
            basket["barcode"],
            basket.get("show_id"),
            basket.get("price"),
            basket.get("client_email"),
            basket.get("client_phone"),
            basket.get("client_name"),
            status,
            json.dumps(basket, ensure_ascii=False),
            basket.get("seat_name"),
            basket.get("seat_id"),
            basket.get("original_price"),
            basket.get("discount_value"),
            basket.get("pdf_url"),
            basket.get("passbook_url"),
            basket.get("series"),
        ),
    )


def first_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


@app.post("/api/telegram/auth")
def telegram_auth(body: TelegramAuthBody):
    user = verify_telegram_init_data(body.init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            upsert_telegram_user(cur, user)
            conn.commit()

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "username": user.get("username"),
            "photo_url": user.get("photo_url"),
        },
    }


@app.post("/api/purchase-session")
def create_purchase_session(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user = get_telegram_user_from_header(x_telegram_init_data)
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

    with get_conn() as conn:
        with conn.cursor() as cur:
            upsert_telegram_user(cur, user)
            cur.execute(
                """
                INSERT INTO purchase_sessions(token, telegram_id, expires_at)
                VALUES (%s, %s, %s)
                """,
                (token, user["id"], expires_at),
            )
            conn.commit()

    return {
        "ok": True,
        "purchase_token": token,
        "qtickets_url": QTICKETS_EVENT_URL,
        "custom": {"purchase_token": token},
    }


@app.get("/api/me/tickets")
def my_tickets(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user = get_telegram_user_from_header(x_telegram_init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            upsert_telegram_user(cur, user)
            cur.execute(
                """
                SELECT
                    t.id,
                    t.barcode,
                    t.ticket_name,
                    t.original_price,
                    t.status,
                    t.checked_at,
                    t.age_verified,
                    t.age_verified_at,
                    t.pdf_url,
                    t.passbook_url,
                    o.id AS order_id,
                    o.payed_at
                FROM qt_orders o
                JOIN qt_tickets t ON t.order_id = o.id
                WHERE o.telegram_id = %s
                ORDER BY o.payed_at DESC NULLS LAST, t.id DESC
                """,
                (user["id"],),
            )
            tickets = cur.fetchall()
            conn.commit()

    return {"ok": True, "tickets": tickets}


@app.post("/api/qtickets/webhook")
async def qtickets_webhook(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_event_type: str | None = Header(default=None, alias="X-Event-Type"),
):
    body = await request.body()
    verify_qtickets_signature(body, x_signature)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event_type = (x_event_type or "").strip().lower()
    event_id = payload.get("event_id")

    if event_id is not None and int(event_id) != QTICKETS_EVENT_ID:
        return {"ok": True, "ignored": True}

    with get_conn() as conn:
        with conn.cursor() as cur:
            save_event(cur, event_type, payload)

            if event_type == "payed":
                upsert_order(cur, payload, "paid")
                for basket in payload.get("baskets") or []:
                    upsert_ticket(cur, payload["id"], basket, "active")

            elif event_type == "deleted":
                upsert_order(cur, payload, "cancelled")
                cur.execute(
                    "UPDATE qt_tickets SET status='cancelled', updated_at=NOW() WHERE order_id=%s",
                    (payload["id"],),
                )

            elif event_type == "refunded":
                upsert_order(cur, payload, "refunded")
                for basket in payload.get("refunded_baskets") or []:
                    if basket.get("id"):
                        cur.execute(
                            "UPDATE qt_tickets SET status='refunded', updated_at=NOW() WHERE id=%s",
                            (basket["id"],),
                        )
                    elif basket.get("barcode"):
                        cur.execute(
                            "UPDATE qt_tickets SET status='refunded', updated_at=NOW() WHERE barcode=%s",
                            (basket["barcode"],),
                        )

            elif event_type == "checked":
                checked = first_dict(payload.get("checked_basket"))
                unchecked = first_dict(payload.get("unchecked_basket"))

                if checked:
                    cur.execute(
                        """
                        UPDATE qt_tickets
                        SET status='used',
                            checked_at=COALESCE(%s, NOW()),
                            updated_at=NOW()
                        WHERE id=%s OR barcode=%s
                        """,
                        (
                            checked.get("checked_at"),
                            checked.get("id"),
                            checked.get("barcode"),
                        ),
                    )

                if unchecked:
                    cur.execute(
                        """
                        UPDATE qt_tickets
                        SET status='active', checked_at=NULL, updated_at=NOW()
                        WHERE id=%s OR barcode=%s
                        """,
                        (unchecked.get("id"), unchecked.get("barcode")),
                    )

            elif event_type in ("created", "updated"):
                upsert_order(cur, payload, "paid" if payload.get("payed") else "pending")

            conn.commit()

    return JSONResponse({"ok": True})


@app.get("/api/tickets/{barcode}")
def ticket_by_barcode(barcode: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_id, barcode, show_id,
                       ticket_name, seat_id, price, original_price,
                       pdf_url, passbook_url,
                       status, checked_at, age_verified, age_verified_at
                FROM qt_tickets
                WHERE barcode=%s
                """,
                (barcode,),
            )
            ticket = cur.fetchone()

    if not ticket:
        raise HTTPException(404, "Ticket not found")

    return ticket


@app.post("/api/tickets/{barcode}/verify-age")
def verify_age(barcode: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE qt_tickets
                SET age_verified=TRUE, age_verified_at=NOW(), updated_at=NOW()
                WHERE barcode=%s
                RETURNING id, barcode, age_verified, age_verified_at
                """,
                (barcode,),
            )
            ticket = cur.fetchone()
            conn.commit()

    if not ticket:
        raise HTTPException(404, "Ticket not found")

    return {"ok": True, "ticket": ticket}
