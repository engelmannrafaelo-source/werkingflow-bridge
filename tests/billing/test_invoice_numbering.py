"""Unit-Tests fuer die getrennten Rechnungs-Nummernkreise (§ 11 UStG)."""
from datetime import datetime, timezone

import pytest

from src.billing.invoice_numbering import (
    FALLBACK_PREFIX,
    TOPUP_PREFIX,
    _seq_name,
    next_invoice_number,
    prefix_for_app,
)

YEAR = datetime.now(timezone.utc).year


class _FakeConn:
    """Minimaler asyncpg-Ersatz: eine In-Memory-Sequenz pro Name."""

    def __init__(self):
        self._seqs: dict[str, int] = {}
        self._current: str | None = None

    async def execute(self, sql: str):
        # CREATE SEQUENCE IF NOT EXISTS <name> ...
        name = sql.split("EXISTS", 1)[1].split()[0]
        self._seqs.setdefault(name, 0)

    async def fetchval(self, sql: str):
        # SELECT nextval('<name>')
        name = sql.split("'")[1]
        self._seqs[name] += 1
        return self._seqs[name]


class TestPrefixForApp:
    def test_known_apps_map_to_own_prefix(self):
        assert prefix_for_app("werking-report") == "WR"
        assert prefix_for_app("werking-energy") == "WE"
        assert prefix_for_app("werking-noise") == "WN"
        assert prefix_for_app("engelmann") == "EG"

    def test_missing_app_id_falls_back(self):
        assert prefix_for_app(None) == FALLBACK_PREFIX
        assert prefix_for_app("") == FALLBACK_PREFIX

    def test_unknown_app_falls_back_without_raising(self):
        assert prefix_for_app("werking-does-not-exist") == FALLBACK_PREFIX


class TestSeqName:
    def test_fallback_keeps_legacy_sequence(self):
        # INV nutzt die historische Sequenz (kein Praefix-Infix) -> keine
        # Kollision mit vor der Umstellung vergebenen INV-Nummern.
        assert _seq_name(FALLBACK_PREFIX, 2026) == "invoice_seq_2026"

    def test_product_prefixes_get_own_sequence(self):
        assert _seq_name("WR", 2026) == "invoice_seq_wr_2026"
        assert _seq_name("WE", 2026) == "invoice_seq_we_2026"
        assert _seq_name(TOPUP_PREFIX, 2026) == "invoice_seq_tu_2026"


class TestNextInvoiceNumber:
    async def test_format_and_increment_per_prefix(self):
        conn = _FakeConn()
        assert await next_invoice_number(conn, "WR") == f"WR-{YEAR}-00001"
        assert await next_invoice_number(conn, "WR") == f"WR-{YEAR}-00002"
        # Ein anderer Kreis laeuft unabhaengig ab 00001.
        assert await next_invoice_number(conn, "WE") == f"WE-{YEAR}-00001"
        assert await next_invoice_number(conn, "WR") == f"WR-{YEAR}-00003"

    async def test_fallback_and_product_never_share_a_sequence(self):
        conn = _FakeConn()
        inv = await next_invoice_number(conn, FALLBACK_PREFIX)
        wr = await next_invoice_number(conn, "WR")
        assert inv == f"INV-{YEAR}-00001"
        assert wr == f"WR-{YEAR}-00001"
        assert inv != wr  # unterschiedliche Kreise, keine Doppelvergabe

    async def test_invalid_prefix_raises(self):
        conn = _FakeConn()
        for bad in ("wr", "W", "TOOLONGX", "W1", "W-", ""):
            with pytest.raises(ValueError):
                await next_invoice_number(conn, bad)
