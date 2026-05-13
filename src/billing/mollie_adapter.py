"""
Mollie-Adapter — Interface fuer die Mollie-API.

Wird vom BillingService aufgerufen. Keine direkten mollie-imports im Rest des Codes.

Implementations:
- LiveMollieAdapter: nutzt mollie-api-python (kommt erst auf Hetzner)
- FakeMollieAdapter: in-memory, deterministische IDs (fuer Mirror + Tests)

Selector: BRIDGE_USE_FAKE_MOLLIE=true -> Fake, sonst Live.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional, Protocol


class MollieAdapter(Protocol):
    async def create_customer(self, email: str, name: str) -> str: ...
    async def create_first_payment(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        description: str,
        redirect_url: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]: ...
    async def create_subscription(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        interval: str,
        description: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]: ...
    async def create_one_time_payment(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        description: str,
        redirect_url: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]: ...
    async def cancel_subscription(self, customer_id: str, subscription_id: str) -> None: ...
    async def get_payment(self, payment_id: str) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# FakeMollieAdapter — in-memory, fuer Mirror + Tests
# ---------------------------------------------------------------------------

class FakeMollieAdapter:
    """Deterministisch genug fuer Smoke-Tests. State global pro Adapter-Instanz."""

    def __init__(self) -> None:
        self._customers: Dict[str, Dict[str, str]] = {}
        self._payments: Dict[str, Dict[str, Any]] = {}
        self._subscriptions: Dict[str, Dict[str, Any]] = {}

    async def create_customer(self, email: str, name: str) -> str:
        cust_id = f"fake_cust_{uuid.uuid4().hex[:12]}"
        self._customers[cust_id] = {"email": email, "name": name}
        return cust_id

    async def create_first_payment(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        description: str,
        redirect_url: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        return self._create_payment(customer_id, amount_eur, metadata)

    async def create_subscription(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        interval: str,
        description: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        sub_id = f"fake_sub_{uuid.uuid4().hex[:12]}"
        self._subscriptions[sub_id] = {
            "customer_id": customer_id,
            "amount_eur": amount_eur,
            "interval": interval,
            "metadata": metadata,
            "status": "active",
        }
        return {"subscriptionId": sub_id}

    async def create_one_time_payment(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        description: str,
        redirect_url: str,
        webhook_url: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        return self._create_payment(customer_id, amount_eur, metadata)

    async def cancel_subscription(self, customer_id: str, subscription_id: str) -> None:
        if subscription_id in self._subscriptions:
            self._subscriptions[subscription_id]["status"] = "canceled"

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        p = self._payments.get(payment_id)
        if not p:
            raise KeyError(f"Unknown payment {payment_id}")
        # In FakeMollie sind alle gestarteten Payments sofort "paid"
        return {**p, "status": "paid"}

    def _create_payment(self, customer_id: str, amount_eur: float, metadata: Dict[str, str]) -> Dict[str, str]:
        pay_id = f"fake_pay_{uuid.uuid4().hex[:12]}"
        self._payments[pay_id] = {
            "id": pay_id,
            "customer_id": customer_id,
            "amount_eur": amount_eur,
            "metadata": metadata,
        }
        return {"paymentId": pay_id, "checkoutUrl": f"https://fake-mollie.test/checkout/{pay_id}"}


# ---------------------------------------------------------------------------
# LiveMollieAdapter — kommt erst auf Hetzner (siehe Phase-4-Deploy)
# ---------------------------------------------------------------------------

class LiveMollieAdapter:
    """Stub. Hetzner-Deploy ergaenzt mollie-api-python-Calls."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("MOLLIE_API_KEY")
        if not self._api_key:
            raise RuntimeError("MOLLIE_API_KEY required for LiveMollieAdapter")

    async def create_customer(self, email: str, name: str) -> str:
        raise NotImplementedError("LiveMollieAdapter: Hetzner-Deploy aktivieren")

    async def create_first_payment(self, **kwargs: Any) -> Dict[str, str]:
        raise NotImplementedError

    async def create_subscription(self, **kwargs: Any) -> Dict[str, str]:
        raise NotImplementedError

    async def create_one_time_payment(self, **kwargs: Any) -> Dict[str, str]:
        raise NotImplementedError

    async def cancel_subscription(self, customer_id: str, subscription_id: str) -> None:
        raise NotImplementedError

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

_adapter_instance: Optional[MollieAdapter] = None


def get_mollie_adapter() -> MollieAdapter:
    """Singleton-Selector. ENV BRIDGE_USE_FAKE_MOLLIE=true -> FakeMollieAdapter."""
    global _adapter_instance
    if _adapter_instance is None:
        if os.getenv("BRIDGE_USE_FAKE_MOLLIE", "false").lower() == "true":
            _adapter_instance = FakeMollieAdapter()
        else:
            _adapter_instance = LiveMollieAdapter()
    return _adapter_instance


def reset_mollie_adapter() -> None:
    """Test-Helper: setzt Singleton zurueck."""
    global _adapter_instance
    _adapter_instance = None
