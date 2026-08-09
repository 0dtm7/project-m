CREATE TABLE IF NOT EXISTS qt_orders (
    id BIGINT PRIMARY KEY,
    uniqid TEXT,
    event_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    payed BOOLEAN,
    payed_at TIMESTAMPTZ,
    price NUMERIC(12,2),
    currency_id TEXT,
    client_email TEXT,
    client_phone TEXT,
    client_name TEXT,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qt_tickets (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES qt_orders(id) ON DELETE CASCADE,
    barcode TEXT UNIQUE NOT NULL,
    show_id BIGINT,
    price NUMERIC(12,2),
    client_email TEXT,
    client_phone TEXT,
    client_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    checked_at TIMESTAMPTZ,
    age_verified BOOLEAN NOT NULL DEFAULT FALSE,
    age_verified_at TIMESTAMPTZ,
    raw JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qt_webhook_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    order_id BIGINT,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qt_tickets_order_id ON qt_tickets(order_id);
CREATE INDEX IF NOT EXISTS idx_qt_tickets_barcode ON qt_tickets(barcode);
CREATE INDEX IF NOT EXISTS idx_qt_orders_event_id ON qt_orders(event_id);
