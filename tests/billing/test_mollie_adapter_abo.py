"""LiveMollieAdapter: Abo-Anlage idempotent (Migration 070, Codex-Befund 4).

Ohne Netz: der mollie-Client wird ersetzt, die Objekte sind echte
mollie-api-python-Objekte (dict-Unterklassen — getattr auf API-Feldnamen
liefert dort still None, deshalb .get()).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("mollie")
from mollie.api.objects.subscription import Subscription  # noqa: E402

from src.billing.mollie_adapter import LiveMollieAdapter  # noqa: E402


class _Seite(list):
    def __init__(self, eintraege, naechste=None):
        super().__init__(eintraege)
        self._naechste = naechste

    def has_next(self):
        return self._naechste is not None

    def get_next(self):
        return self._naechste


def _adapter(resource):
    a = LiveMollieAdapter.__new__(LiveMollieAdapter)
    a._client = MagicMock()
    a._client.customer_subscriptions.with_parent_id.return_value = resource
    return a


def _abo(i, status, erstzahlung):
    return Subscription({"id": i, "status": status,
                         "metadata": {"firstPaymentId": erstzahlung}}, None)


async def test_create_subscription_gibt_idempotenzschluessel_weiter():
    res = MagicMock()
    res.create.return_value = Subscription({"id": "sub_1"}, None)
    erg = await _adapter(res).create_subscription(
        customer_id="cst_1", amount_eur=10, interval="1 month", description="d",
        webhook_url="https://w", metadata={"firstPaymentId": "tr_1"},
        idempotency_key="abo-erstzahlung-tr_1",
    )
    assert erg == {"subscriptionId": "sub_1"}
    assert res.create.call_args.kwargs["idempotency_key"] == "abo-erstzahlung-tr_1"
    assert res.create.call_args.args[0]["metadata"] == {"firstPaymentId": "tr_1"}


async def test_find_sucht_ueber_seiten_und_ignoriert_gekuendigte():
    res = MagicMock()
    seite2 = _Seite([_abo("sub_alt", "canceled", "tr_9"), _abo("sub_ok", "active", "tr_9")])
    res.list.return_value = _Seite([_abo("sub_x", "active", "tr_1")], naechste=seite2)
    a = _adapter(res)
    assert await a.find_subscription_for_first_payment("cst_1", "tr_9") == "sub_ok"
    assert await a.find_subscription_for_first_payment("cst_1", "tr_unbekannt") is None
