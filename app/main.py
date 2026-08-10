import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .db import get_conn

app = FastAPI(title="PROJECT M backend", version="0.5.2")

QTICKETS_WEBHOOK_SECRET = os.getenv("QTICKETS_WEBHOOK_SECRET", "")
QTICKETS_EVENT_ID = int(os.getenv("QTICKETS_EVENT_ID", "251223"))
QTICKETS_SHOW_ID = int(os.getenv("QTICKETS_SHOW_ID", "950276"))
QTICKETS_API_TOKEN = os.getenv("QTICKETS_API_TOKEN", "")
CHECKIN_TEST_MODE = os.getenv("CHECKIN_TEST_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
STAFF_CHECK_PIN = os.getenv("STAFF_CHECK_PIN", "")
STAFF_TELEGRAM_IDS = {
    int(x.strip()) for x in os.getenv("STAFF_TELEGRAM_IDS", "").split(",")
    if x.strip().isdigit()
}

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://project-m-a8gu.onrender.com"
).rstrip("/")

MINI_APP_URL = f"{PUBLIC_BASE_URL}/app"
TELEGRAM_WEBHOOK_URL = f"{PUBLIC_BASE_URL}/api/telegram/webhook"
QTICKETS_EVENT_URL = f"https://qtickets.ru/event/{QTICKETS_EVENT_ID}"

APP_HTML = Path(__file__).with_name("index.html")
STAFF_HTML = Path(__file__).with_name("staff.html")

SURGUT_TZ = timezone(timedelta(hours=5))
MOSCOW_TZ = timezone(timedelta(hours=3))

DOORS_OPEN = datetime(2026, 9, 5, 16, 0, tzinfo=SURGUT_TZ)
EVENT_DATE = datetime(2026, 9, 5, 18, 0, tzinfo=SURGUT_TZ)
ADULT_ONLY_TIME = datetime(2026, 9, 5, 22, 0, tzinfo=SURGUT_TZ)
EVENT_END = datetime(2026, 9, 6, 2, 0, tzinfo=SURGUT_TZ)


class TelegramAuthBody(BaseModel):
    init_data: str


class StaffLoginBody(BaseModel):
    pin: str


class AgeBody(BaseModel):
    age_group: str


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "project-m-backend",
        "app": "/app",
        "staff": "/staff",
        "version": "0.5.2",
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "project-m-backend", "version": "0.5.2"}


@app.get("/app")
def mini_app():
    return FileResponse(APP_HTML)


@app.get("/staff")
def staff_app():
    return FileResponse(STAFF_HTML)


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

    if text.startswith("/start"):
        try:
            send_project_m_start(chat_id)
        except Exception:
            pass
        return {"ok": True}

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
# QTickets REST API
# ---------------------------

def qtickets_api(method: str, path: str, payload: dict[str, Any] | None = None):
    if not QTICKETS_API_TOKEN:
        raise HTTPException(503, "QTICKETS_API_TOKEN is not configured")

    url = f"https://qtickets.ru/api/rest/v1/{path.lstrip('/')}"
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {QTICKETS_API_TOKEN}",
            "User-Agent": "PROJECT-M/0.5",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        raise HTTPException(
            status_code=502,
            detail=f"QTickets API error {exc.code}: {detail[:300]}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"QTickets API unavailable: {type(exc).__name__}",
        )


# ---------------------------
# Staff auth
# ---------------------------

def staff_secret() -> bytes:
    material = f"{TELEGRAM_BOT_TOKEN}|{STAFF_CHECK_PIN}|project-m-staff"
    return hashlib.sha256(material.encode("utf-8")).digest()


def make_staff_session() -> str:
    expires = int(time.time()) + 12 * 60 * 60
    payload = str(expires)
    signature = hmac.new(
        staff_secret(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_staff_session(value: str | None) -> None:
    if not STAFF_CHECK_PIN:
        raise HTTPException(503, "STAFF_CHECK_PIN is not configured")
    if not value or "." not in value:
        raise HTTPException(401, "Staff login required")

    expires_raw, signature = value.split(".", 1)

    try:
        expires = int(expires_raw)
    except ValueError:
        raise HTTPException(401, "Invalid staff session")

    if expires < int(time.time()):
        raise HTTPException(401, "Staff session expired")

    expected = hmac.new(
        staff_secret(),
        expires_raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid staff session")



def verify_staff_telegram(init_data: str | None) -> dict[str, Any]:
    user = verify_telegram_init_data(init_data or "")
    if int(user["id"]) not in STAFF_TELEGRAM_IDS:
        raise HTTPException(403, "Нет доступа к режиму сотрудника")
    return user


def verify_staff_access(
    pm_staff: str | None,
    telegram_init_data: str | None,
) -> str:
    if telegram_init_data:
        user = verify_staff_telegram(telegram_init_data)
        return f"telegram:{user['id']}"

    verify_staff_session(pm_staff)
    return "pin"


@app.get("/api/staff/telegram/session")
def staff_telegram_session(
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
):
    user = verify_staff_telegram(x_telegram_init_data)
    return {
        "ok": True,
        "staff": True,
        "telegram_id": user["id"],
    }


@app.post("/api/staff/login")
def staff_login(body: StaffLoginBody, response: Response):
    if not STAFF_CHECK_PIN:
        raise HTTPException(503, "STAFF_CHECK_PIN is not configured")

    if not secrets.compare_digest(body.pin.strip(), STAFF_CHECK_PIN):
        raise HTTPException(401, "Неверный PIN")

    response.set_cookie(
        key="pm_staff",
        value=make_staff_session(),
        max_age=12 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.post("/api/staff/logout")
def staff_logout(response: Response):
    response.delete_cookie("pm_staff", path="/")
    return {"ok": True}


@app.get("/api/staff/session")
def staff_session(pm_staff: str | None = Cookie(default=None)):
    verify_staff_session(pm_staff)
    return {"ok": True}


def access_decision(age_group: str | None):
    now = datetime.now(SURGUT_TZ)

    if not age_group:
        return None, "Проверь документ гостя."

    if now < DOORS_OPEN:
        if CHECKIN_TEST_MODE:
            if age_group == "18+":
                return True, "ТЕСТОВЫЙ РЕЖИМ: 18+ подтверждён."
            if age_group == "16-17":
                return True, "ТЕСТОВЫЙ РЕЖИМ: 16–17 подтверждено."
        return None, "Возраст подтверждён. Вход ещё не открыт."

    if now >= EVENT_END:
        return False, "Мероприятие уже завершено."

    if age_group == "18+":
        return True, "18+ подтверждён. Допуск разрешён."

    if age_group == "16-17":
        if now < ADULT_ONLY_TIME:
            return True, "16–17 подтверждено. До 22:00 допуск разрешён."
        return False, "16–17. После 22:00 НЕ ДОПУСКАТЬ."

    return None, "Неизвестный возрастной статус."


def fetch_ticket_by_code(cur, code: str):
    cur.execute(
        """
        SELECT
            t.id,
            t.order_id,
            t.barcode,
            t.show_id,
            t.ticket_name,
            t.status,
            t.checked_at,
            t.age_verified,
            t.age_verified_at,
            t.age_group,
            o.event_id,
            o.status AS order_status
        FROM qt_tickets t
        JOIN qt_orders o ON o.id = t.order_id
        WHERE t.barcode = %s
           OR t.id::text = %s
        LIMIT 1
        """,
        (code, code),
    )
    return cur.fetchone()


def decorate_staff_ticket(ticket: dict[str, Any]):
    allowed, reason = access_decision(ticket.get("age_group"))
    result = dict(ticket)
    result["access_now"] = allowed
    result["access_reason"] = reason
    return result


@app.get("/api/staff/tickets/{code}")
def staff_get_ticket(
    code: str,
    pm_staff: str | None = Cookie(default=None),
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
):
    verify_staff_access(pm_staff, x_telegram_init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            ticket = fetch_ticket_by_code(cur, code.strip())

    if not ticket:
        raise HTTPException(404, "Билет не найден")

    return {"ok": True, "ticket": decorate_staff_ticket(ticket)}


@app.post("/api/staff/tickets/{code}/age")
def staff_set_age(
    code: str,
    body: AgeBody,
    pm_staff: str | None = Cookie(default=None),
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
):
    checked_by = verify_staff_access(pm_staff, x_telegram_init_data)

    if body.age_group not in ("16-17", "18+"):
        raise HTTPException(400, "Недопустимый возрастной статус")

    with get_conn() as conn:
        with conn.cursor() as cur:
            ticket = fetch_ticket_by_code(cur, code.strip())
            if not ticket:
                raise HTTPException(404, "Билет не найден")

            cur.execute(
                """
                UPDATE qt_tickets
                SET
                    age_group=%s,
                    age_verified=TRUE,
                    age_verified_at=NOW(),
                    age_verified_by='staff',
                    updated_at=NOW()
                WHERE id=%s
                """,
                (body.age_group, ticket["id"]),
            )

            cur.execute(
                """
                INSERT INTO age_check_events(ticket_id, age_group, action, checked_by)
                VALUES (%s, %s, 'verify', %s)
                """,
                (ticket["id"], body.age_group, checked_by),
            )

            conn.commit()
            ticket = fetch_ticket_by_code(cur, str(ticket["id"]))

    return {"ok": True, "ticket": decorate_staff_ticket(ticket)}


@app.delete("/api/staff/tickets/{code}/age")
def staff_reset_age(
    code: str,
    pm_staff: str | None = Cookie(default=None),
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
):
    checked_by = verify_staff_access(pm_staff, x_telegram_init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            ticket = fetch_ticket_by_code(cur, code.strip())
            if not ticket:
                raise HTTPException(404, "Билет не найден")

            cur.execute(
                """
                UPDATE qt_tickets
                SET
                    age_group=NULL,
                    age_verified=FALSE,
                    age_verified_at=NULL,
                    age_verified_by=NULL,
                    updated_at=NOW()
                WHERE id=%s
                """,
                (ticket["id"],),
            )

            cur.execute(
                """
                INSERT INTO age_check_events(ticket_id, age_group, action, checked_by)
                VALUES (%s, NULL, 'reset', %s)
                """,
                (ticket["id"], checked_by),
            )

            conn.commit()
            ticket = fetch_ticket_by_code(cur, str(ticket["id"]))

    return {"ok": True, "ticket": decorate_staff_ticket(ticket)}


@app.post("/api/staff/tickets/{code}/admit")
def staff_admit_ticket(
    code: str,
    pm_staff: str | None = Cookie(default=None),
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
):
    checked_by = verify_staff_access(pm_staff, x_telegram_init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            ticket = fetch_ticket_by_code(cur, code.strip())

            if not ticket:
                raise HTTPException(404, "Билет не найден")

            if int(ticket.get("event_id") or 0) != QTICKETS_EVENT_ID:
                raise HTTPException(400, "Билет относится к другому мероприятию")

            if ticket.get("order_status") in ("cancelled", "refunded"):
                raise HTTPException(409, "Билет отменён или возвращён")

            if ticket.get("status") == "used" or ticket.get("checked_at"):
                raise HTTPException(409, "Билет уже был использован")

            allowed, reason = access_decision(ticket.get("age_group"))
            if allowed is not True:
                raise HTTPException(409, reason)

            barcode = str(ticket.get("barcode") or "").strip()
            if not barcode:
                raise HTTPException(400, "У билета отсутствует barcode")

            show_id = int(ticket.get("show_id") or QTICKETS_SHOW_ID)

            # Сначала сверяем актуальное состояние в QTickets.
            remote = qtickets_api(
                "GET",
                f"shows/{show_id}/barcode/{barcode}",
            )

            remote_checked_at = remote.get("checked_at") if isinstance(remote, dict) else None
            if remote_checked_at:
                cur.execute(
                    """
                    UPDATE qt_tickets
                    SET status='used',
                        checked_at=%s,
                        updated_at=NOW()
                    WHERE id=%s
                    """,
                    (remote_checked_at, ticket["id"]),
                )
                conn.commit()
                raise HTTPException(
                    409,
                    f"Билет уже отмечен на входе: {remote_checked_at}",
                )

            checked_at = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

            result = qtickets_api(
                "POST",
                f"shows/{show_id}/barcode/{barcode}",
                {"checked_at": checked_at},
            )

            saved_checked_at = (
                result.get("checked_at")
                if isinstance(result, dict)
                else None
            ) or checked_at

            cur.execute(
                """
                UPDATE qt_tickets
                SET status='used',
                    checked_at=%s,
                    updated_at=NOW()
                WHERE id=%s
                """,
                (saved_checked_at, ticket["id"]),
            )

            cur.execute(
                """
                INSERT INTO entry_check_events(
                    ticket_id,
                    barcode,
                    show_id,
                    action,
                    checked_by,
                    qtickets_response
                )
                VALUES (%s, %s, %s, 'admit', %s, %s::jsonb)
                """,
                (
                    ticket["id"],
                    barcode,
                    show_id,
                    checked_by,
                    json.dumps(result, ensure_ascii=False),
                ),
            )

            conn.commit()
            ticket = fetch_ticket_by_code(cur, str(ticket["id"]))

    decorated = decorate_staff_ticket(ticket)
    decorated["access_now"] = False
    decorated["access_reason"] = "ГОСТЬ ПРОПУЩЕН. Билет использован."

    return {
        "ok": True,
        "admitted": True,
        "ticket": decorated,
        "qtickets": result,
    }


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


def extract_purchase_token_from_utm(value: Any) -> str | None:
    """
    Fallback channel for purchases opened as a normal QTickets page.
    We put pm_<purchase_token> into a standard UTM value.
    QTickets stores UTM data in the order/webhook payload.
    """
    value = normalize_custom(value)

    if isinstance(value, dict):
        for item in value.values():
            token = extract_purchase_token_from_utm(item)
            if token:
                return token
        return None

    if isinstance(value, list):
        for item in value:
            token = extract_purchase_token_from_utm(item)
            if token:
                return token
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("pm_") and len(text) > 3:
            return text[3:]

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
    purchase_token = (
        extract_purchase_token(custom)
        or extract_purchase_token_from_utm(payload.get("utm"))
    )
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

    fallback_query = urlencode({
        "utm_source": "project_m",
        "utm_medium": "telegram_miniapp",
        "utm_campaign": f"pm_{token}",
    })
    purchase_url = f"{QTICKETS_EVENT_URL}?{fallback_query}"

    return {
        "ok": True,
        "purchase_token": token,
        "qtickets_url": purchase_url,
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
                    t.age_group,
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
                       status, checked_at, age_verified, age_verified_at,
                       age_group
                FROM qt_tickets
                WHERE barcode=%s
                """,
                (barcode,),
            )
            ticket = cur.fetchone()

    if not ticket:
        raise HTTPException(404, "Ticket not found")

    return ticket
