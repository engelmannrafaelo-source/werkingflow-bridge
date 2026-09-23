"""Aufladung per Mollie: die Gutschrift muss an einer ECHTEN Datenbank durchgehen.

Gemessen 23.09.2026 an der dev-Bridge: die bezahlte Testzahlung
tr_bDcNDGUB3ax8Atso6kBXJ (50 EUR) wurde nie gutgeschrieben, der Webhook
antwortete bei jedem Aufruf 500. Ursache war die Lot-Zeile in `_credit_topup`:
`VALUES (..., $3, $3 + INTERVAL '12 months', ...)`. Postgres leitet aus
`$3 + INTERVAL` fuer $3 den Typ interval ab und aus der Spalte timestamptz —
AmbiguousParameterError, Rollback, 500. Seit e993d0e (05.07.) ging so jede
Aufladung verloren.

Die Tests in test_billing_service.py bilden den Pool nach und fuehren kein SQL
aus; darum konnten sie das nicht sehen. Hier zwei Wachen:

1. statisch, ohne Datenbank: kein ungetypter Parameter wird in `src/` mit
   INTERVAL verrechnet (laeuft ueberall);
2. `_credit_topup` gegen einen echten Postgres. Braucht `BRIDGE_TEST_PG_URL`
   (z. B. ein Wegwerf-Container `postgres:16-alpine`); ohne sie wird der Test
   ausdruecklich als uebersprungen gemeldet, nie still gruen.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# `$3 + INTERVAL`, `$2 - interval` — ein Parameter ohne `::typ` direkt vor der
# Intervall-Rechnung. Mit `$3::timestamptz + INTERVAL` ist der Typ eindeutig.
_UNGETYPT_MIT_INTERVALL = re.compile(r"\$\d+\s*[+-]\s*interval\b", re.IGNORECASE)


def test_kein_ungetypter_parameter_mit_interval_in_src():
    funde = []
    for datei in sorted(SRC.rglob("*.py")):
        for nr, zeile in enumerate(datei.read_text(encoding="utf-8").splitlines(), 1):
            if not zeile.lstrip().startswith("#") and _UNGETYPT_MIT_INTERVALL.search(zeile):
                funde.append(f"{datei.relative_to(SRC.parent)}:{nr}: {zeile.strip()}")
    assert not funde, (
        "Ungetypter SQL-Parameter vor INTERVAL — Postgres leitet dafuer interval ab "
        "und bricht mit AmbiguousParameterError ab. `$n::timestamptz` schreiben:\n"
        + "\n".join(funde)
    )


PG_URL = os.environ.get("BRIDGE_TEST_PG_URL")

_SCHEMA = """
CREATE TABLE users (id UUID PRIMARY KEY);
CREATE TABLE mollie_customers (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    mollie_customer_id VARCHAR(128) NOT NULL,
    email VARCHAR(255), name VARCHAR(255), created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE credit_purchases (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID         NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    pack_eur           NUMERIC(8,2) NOT NULL CHECK (pack_eur > 0),
    paid_at            TIMESTAMPTZ  NOT NULL,
    mollie_customer_id VARCHAR(128) NOT NULL,
    mollie_payment_id  VARCHAR(128) NOT NULL UNIQUE
);
"""


@pytest.mark.skipif(not PG_URL, reason="BRIDGE_TEST_PG_URL fehlt — Gutschrift nicht gegen echten Postgres geprueft")
async def test_credit_topup_schreibt_gutschrift_in_echte_datenbank():
    import asyncpg

    from src.billing import billing_service

    schema = f"topup_probe_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2,
                                     server_settings={"search_path": schema})
    try:
        lots_ddl = (SRC.parent / "docker/migrations/035_topup_lots.sql").read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            await conn.execute(lots_ddl)  # die echte Migration, nicht nachgebaut
            user_id = uuid.uuid4()
            await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
            await conn.execute(
                "INSERT INTO mollie_customers (user_id, mollie_customer_id) VALUES ($1, 'cst_probe')",
                user_id,
            )

        with patch.object(billing_service, "get_pool", return_value=pool), \
             patch.object(billing_service, "log_billing_event", new=AsyncMock()), \
             patch.object(billing_service, "auto_create_invoice", new=AsyncMock()):
            erst = await billing_service._credit_topup(str(user_id), 50.0, "tr_probe")
            zweit = await billing_service._credit_topup(str(user_id), 50.0, "tr_probe")

        assert erst == {"alreadyCredited": False, "balanceEur": 50.0}
        assert zweit == {"alreadyCredited": True, "balanceEur": 50.0}
        async with pool.acquire() as conn:
            lot = await conn.fetchrow(
                "SELECT amount_eur, expires_at - purchased_at AS dauer FROM user_topup_lots WHERE user_id = $1",
                user_id,
            )
        assert float(lot["amount_eur"]) == 50.0
        assert 365 <= lot["dauer"].days <= 366  # 12 Monate ab Kaufzeitpunkt
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()
