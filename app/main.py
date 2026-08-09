import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import get_conn

app = FastAPI(title="PROJECT M backend", version="0.1.0")

QTICKETS_WEBHOOK_SECRET = os.getenv("QTICKETS_WEBHOOK_SECRET", "")
QTICKETS_EVENT_ID = int(os.getenv("QTICKETS_EVENT_ID", "251223"))


@app.get("/health")
def health():
    return {"ok": True, "service": "project-m-backend"}


def verify_signature(body: bytes, signature: str | None) -> None:
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


def upsert_order(cur, payload: dict[str, Any], status: str):
    email, phone, name = client_fields(payload)
    cur.execute(
        """
        INSERT INTO qt_orders (
            id, uniqid, event_id, status, payed, payed_at, price,
            currency_id, client_email, client_phone, client_name, raw, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::jsonb, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            uniqid = EXCLUDED.uniqid,
            event_id = EXCLUDED.event_id,
            status = EXCLUDED.status,
            payed = EXCLUDED.payed,
            payed_at = EXCLUDED.payed_at,
            price = EXCLUDED.price,
            currency_id = EXCLUDED.currency_id,
            client_email = EXCLUDED.client_email,
            client_phone = EXCLUDED.client_phone,
            client_name = EXCLUDED.client_name,
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
            payload.get("currency_id"),
            email,
            phone,
            name,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def upsert_ticket(cur, order_id: int, basket: dict[str, Any], status: str = "active"):
    cur.execute(
        """
        INSERT INTO qt_tickets (
            id, order_id, barcode, show_id, price,
            client_email, client_phone, client_name,
            status, raw, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
        ON CONFLICT (id) DO UPDATE SET
            barcode = EXCLUDED.barcode,
            show_id = EXCLUDED.show_id,
            price = EXCLUDED.price,
            client_email = EXCLUDED.client_email,
            client_phone = EXCLUDED.client_phone,
            client_name = EXCLUDED.client_name,
            status = EXCLUDED.status,
            raw = EXCLUDED.raw,
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
        ),
    )


def first_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


@app.post("/api/qtickets/webhook")
async def qtickets_webhook(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_event_type: str | None = Header(default=None, alias="X-Event-Type"),
):
    body = await request.body()
    verify_signature(body, x_signature)

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
                        SET status='used', checked_at=NOW(), updated_at=NOW()
                        WHERE id=%s OR barcode=%s
                        """,
                        (checked.get("id"), checked.get("barcode")),
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
                upsert_order(
                    cur,
                    payload,
                    "paid" if payload.get("payed") else "pending",
                )

            conn.commit()

    return JSONResponse({"ok": True})


@app.get("/api/tickets/{barcode}")
def ticket_by_barcode(barcode: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_id, barcode, show_id, price, client_name,
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
