"""
Mollie-Adapter — Interface fuer die Mollie-API.

Wird vom BillingService aufgerufen. Keine direkten mollie-imports im Rest des Codes.

Implementations:
- LiveMollieAdapter: nutzt mollie-api-python — echte Checkouts/Subscriptions
- FakeMollieAdapter: in-memory, deterministische IDs (fuer Mirror + Tests)

Selector: BRIDGE_USE_FAKE_MOLLIE=true -> Fake, sonst Live.
"""
from __future__ import annotations

import asyncio
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
        return {**p, "status": "paid", "subscription_id": p.get("subscription_id")}

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
# LiveMollieAdapter — echte Mollie-API via mollie-api-python
# ---------------------------------------------------------------------------

class LiveMollieAdapter:
    """Real Mollie integration via mollie-api-python.

    The mollie client is synchronous; every call is dispatched to a worker
    thread (asyncio.to_thread) so it never blocks the event loop. Mollie API
    errors are allowed to propagate — billing fails loud, it never pretends
    a payment succeeded.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        api_key = api_key or os.getenv("MOLLIE_API_KEY")
        if not api_key:
            raise RuntimeError("MOLLIE_API_KEY required for LiveMollieAdapter")
        # Imported lazily so Fake-only environments (tests, mirror) do not
        # need mollie-api-python installed.
        from mollie.api.client import Client
        self._client = Client()
        self._client.set_api_key(api_key)

    @staticmethod
    def _amount(amount_eur: float) -> Dict[str, str]:
        # Mollie requires the value as a string with exactly two decimals.
        return {"currency": "EUR", "value": f"{amount_eur:.2f}"}

    async def create_customer(self, email: str, name: str) -> str:
        customer = await asyncio.to_thread(
            self._client.customers.create, {"name": name, "email": email}
        )
        return customer.id

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
        return await self._create_payment(
            customer_id=customer_id, amount_eur=amount_eur, description=description,
            redirect_url=redirect_url, webhook_url=webhook_url, metadata=metadata,
            sequence_type="first",
        )

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
        return await self._create_payment(
            customer_id=customer_id, amount_eur=amount_eur, description=description,
            redirect_url=redirect_url, webhook_url=webhook_url, metadata=metadata,
            sequence_type="oneoff",
        )

    async def _create_payment(
        self,
        *,
        customer_id: str,
        amount_eur: float,
        description: str,
        redirect_url: str,
        webhook_url: str,
        metadata: Dict[str, str],
        sequence_type: str,
    ) -> Dict[str, str]:
        payment = await asyncio.to_thread(self._client.payments.create, {
            "amount": self._amount(amount_eur),
            "description": description,
            "redirectUrl": redirect_url,
            "webhookUrl": webhook_url,
            "metadata": metadata,
            "customerId": customer_id,
            "sequenceType": sequence_type,
        })
        return {"paymentId": payment.id, "checkoutUrl": payment.checkout_url}

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
        subscription = await asyncio.to_thread(
            self._client.customer_subscriptions.with_parent_id(customer_id).create,
            {
                "amount": self._amount(amount_eur),
                "interval": interval,
                "description": description,
                "webhookUrl": webhook_url,
                "metadata": metadata,
            },
        )
        return {"subscriptionId": subscription.id}

    async def cancel_subscription(self, customer_id: str, subscription_id: str) -> None:
        await asyncio.to_thread(
            self._client.customer_subscriptions.with_parent_id(customer_id).delete,
            subscription_id,
        )

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        payment = await asyncio.to_thread(self._client.payments.get, payment_id)
        amount = getattr(payment, "amount", None)
        return {
            "id": payment.id,
            "status": payment.status,
            "customer_id": getattr(payment, "customer_id", None),
            "subscription_id": getattr(payment, "subscriptionId", None),
            "metadata": payment.metadata or {},
            "amount_eur": float(amount["value"]) if amount else 0.0,
        }


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
