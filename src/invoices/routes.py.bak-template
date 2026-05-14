"""
Invoices endpoints — bridge becomes the invoice-of-record for all apps.

POST   /v1/invoices                    create (admin or service)
GET    /v1/invoices                    list (admin, with filters)
GET    /v1/invoices/{id}               detail (self-or-admin)
GET    /v1/invoices/{id}/html          render HTML invoice (self-or-admin)
PATCH  /v1/invoices/{id}               status / metadata updates (admin)

PDF + Email-Send are phase 2 (weasyprint + Resend integration).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from src.api_auth import (
    require_admin,
    require_jwt_or_service,
    require_self_or_admin,
    AuthClaims,
)
from src.db.client import get_pool

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])

_ALLOWED_STATUS = {"draft", "issued", "paid", "cancelled", "refunded"}


def _dec(v: Any) -> str:
    """Serialize NUMERIC to plain string with 2 decimals (no float drift)."""
    if v is None:
        return "0.00"
    return f"{Decimal(v):.2f}"


def _row(r: Any) -> Dict[str, Any]:
    def _maybe(v: Any) -> Any:
        if v is None or isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except Exception:
            return v
    return {
        "id": str(r["id"]),
        "invoiceNumber": r["invoice_number"],
        "userId": str(r["user_id"]),
        "tenantId": r["tenant_id"],
        "subscriptionId": str(r["subscription_id"]) if r["subscription_id"] else None,
        "creditPurchaseId": str(r["credit_purchase_id"]) if r["credit_purchase_id"] else None,
        "molliePaymentId": r["mollie_payment_id"],
        "status": r["status"],
        "subtotalEur": _dec(r["subtotal_eur"]),
        "taxRate": _dec(r["tax_rate"]),
        "taxEur": _dec(r["tax_eur"]),
        "totalEur": _dec(r["total_eur"]),
        "currency": r["currency"],
        "lineItems": _maybe(r["line_items"]) or [],
        "billingAddress": _maybe(r["billing_address"]),
        "issuedAt": r["issued_at"].isoformat() if r["issued_at"] else None,
        "paidAt": r["paid_at"].isoformat() if r["paid_at"] else None,
        "dueAt": r["due_at"].isoformat() if r["due_at"] else None,
        "cancelledAt": r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
        "refundedAt": r["refunded_at"].isoformat() if r["refunded_at"] else None,
        "sentAt": r["sent_at"].isoformat() if r["sent_at"] else None,
        "notes": r["notes"],
        "metadata": _maybe(r["metadata"]) or {},
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Invoice number generation (deterministic, gap-free per year)
# ---------------------------------------------------------------------------

async def _next_invoice_number(conn: asyncpg.Connection) -> str:
    """
    Returns INV-<year>-<5-digit-seq>, e.g. INV-2026-00001.

    Uses a per-year sequence. Sequences are NOT transactional so even if the
    caller rolls back, the number is "consumed" — which is intentional: we
    never want to re-use an invoice number that was even briefly visible.
    """
    year = datetime.now(timezone.utc).year
    seq_name = f"invoice_seq_{year}"
    # Create sequence on the fly if year just rolled over and DDL wasn't run.
    await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq_name} START 1 INCREMENT 1")
    n = await conn.fetchval(f"SELECT nextval('{seq_name}')")
    return f"INV-{year}-{int(n):05d}"


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------

class LineItem(BaseModel):
    description: str = Field(min_length=1, max_length=512)
    quantity: float = Field(gt=0)
    unitPriceEur: float = Field(ge=0)
    totalEur: float = Field(ge=0)
    metadata: Dict[str, Any] = {}


class BillingAddress(BaseModel):
    name: str
    street: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    country: Optional[str] = None
    vatId: Optional[str] = None


class InvoiceCreate(BaseModel):
    userId: str
    tenantId: Optional[str] = None
    subscriptionId: Optional[str] = None
    creditPurchaseId: Optional[str] = None
    molliePaymentId: Optional[str] = None
    status: str = "draft"
    lineItems: List[LineItem]
    billingAddress: Optional[BillingAddress] = None
    taxRate: float = Field(default=20.0, ge=0, le=100)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    notes: Optional[str] = None
    metadata: Dict[str, Any] = {}
    dueAt: Optional[str] = None


class InvoiceUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_invoice(
    body: InvoiceCreate,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if body.status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail=f"Unknown status: {body.status}")
    if not body.lineItems:
        raise HTTPException(status_code=400, detail="lineItems must not be empty")

    # Compute totals server-side to keep client-trust low.
    subtotal = sum(Decimal(str(li.totalEur)) for li in body.lineItems)
    tax_rate = Decimal(str(body.taxRate))
    tax = (subtotal * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax).quantize(Decimal("0.01"))

    line_items_json = [li.model_dump() for li in body.lineItems]
    due_at_dt = datetime.fromisoformat(body.dueAt.replace("Z", "+00:00")) if body.dueAt else None

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice_number = await _next_invoice_number(conn)
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO invoices
                      (invoice_number, user_id, tenant_id, subscription_id,
                       credit_purchase_id, mollie_payment_id, status,
                       subtotal_eur, tax_rate, tax_eur, total_eur, currency,
                       line_items, billing_address, due_at, notes, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7,
                            $8, $9, $10, $11, $12,
                            $13::jsonb, $14::jsonb, $15, $16, $17::jsonb)
                    RETURNING *
                    """,
                    invoice_number,
                    uuid.UUID(body.userId),
                    body.tenantId,
                    uuid.UUID(body.subscriptionId) if body.subscriptionId else None,
                    uuid.UUID(body.creditPurchaseId) if body.creditPurchaseId else None,
                    body.molliePaymentId,
                    body.status,
                    subtotal, tax_rate, tax, total, body.currency,
                    json.dumps(line_items_json),
                    json.dumps(body.billingAddress.model_dump()) if body.billingAddress else None,
                    due_at_dt,
                    body.notes,
                    json.dumps(body.metadata or {}),
                )
            except asyncpg.UniqueViolationError:
                # Either invoice_number race (shouldn't happen — sequence is unique)
                # or molliePaymentId collision (the actual idempotency guard).
                if body.molliePaymentId:
                    existing = await conn.fetchrow(
                        "SELECT * FROM invoices WHERE mollie_payment_id = $1",
                        body.molliePaymentId,
                    )
                    if existing:
                        return _row(existing)
                raise HTTPException(status_code=409, detail="Invoice conflict (duplicate)")
    return _row(row)


@router.get("")
async def list_invoices(
    userId: Optional[str] = Query(default=None),
    tenantId: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    if not claims.is_admin:
        # Non-admin sees own invoices only
        if userId and userId != claims.user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        userId = claims.user_id

    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if userId:    add("user_id = $$", uuid.UUID(userId))
    if tenantId:  add("tenant_id = $$", tenantId)
    if status:
        if status not in _ALLOWED_STATUS:
            raise HTTPException(status_code=400, detail=f"Unknown status: {status}")
        add("status = $$", status)
    if since:     add("issued_at >= $$", since)
    if until:     add("issued_at <= $$", until)

    sql = "SELECT * FROM invoices"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT $" + str(len(args) + 1)
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {"items": [_row(r) for r in rows], "count": len(rows)}


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT * FROM invoices WHERE id = $1",
            uuid.UUID(invoice_id),
        )
    if not r:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    if not claims.is_admin and str(r["user_id"]) != claims.user_id:
        raise HTTPException(status_code=403, detail="Forbidden: not your invoice")
    return _row(r)


@router.patch("/{invoice_id}")
async def update_invoice(
    invoice_id: str,
    body: InvoiceUpdate,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if body.status is None and body.notes is None and body.metadata is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    if body.status is not None and body.status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail=f"Unknown status: {body.status}")

    sets: List[str] = []
    args: List[Any] = []

    def add_set(col: str, val: Any) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    if body.status is not None:
        add_set("status", body.status)
        # Mark transition timestamps
        if body.status == "issued":
            sets.append("issued_at = COALESCE(issued_at, NOW())")
        elif body.status == "paid":
            sets.append("paid_at = COALESCE(paid_at, NOW())")
        elif body.status == "cancelled":
            sets.append("cancelled_at = COALESCE(cancelled_at, NOW())")
        elif body.status == "refunded":
            sets.append("refunded_at = COALESCE(refunded_at, NOW())")
    if body.notes is not None:
        add_set("notes", body.notes)
    if body.metadata is not None:
        add_set("metadata", json.dumps(body.metadata))
    sets.append("updated_at = NOW()")

    args.append(uuid.UUID(invoice_id))
    sql = f"UPDATE invoices SET {', '.join(sets)} WHERE id = ${len(args)} RETURNING *"

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(sql, *args)
    if not r:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    return _row(r)


# ---------------------------------------------------------------------------
# HTML rendering — phase-1 stand-in for PDF (browser print or copy as basis)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>Rechnung {invoice_number}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; color: #222; max-width: 800px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  .meta {{ color: #666; font-size: 12px; }}
  .addr {{ white-space: pre-line; margin: 24px 0; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
  th, td {{ padding: 8px 6px; border-bottom: 1px solid #ddd; text-align: left; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .totals {{ margin: 12px 0 24px; text-align: right; font-size: 13px; }}
  .totals .grand {{ font-size: 16px; font-weight: 700; padding-top: 6px; border-top: 2px solid #222; display: inline-block; min-width: 200px; }}
  .status {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .status-paid {{ background: #d4edda; color: #155724; }}
  .status-issued {{ background: #cce5ff; color: #004085; }}
  .status-draft {{ background: #e2e3e5; color: #383d41; }}
  .status-cancelled, .status-refunded {{ background: #f8d7da; color: #721c24; }}
  .notes {{ font-size: 12px; color: #666; margin-top: 24px; padding-top: 12px; border-top: 1px solid #eee; }}
</style></head><body>
<h1>Rechnung {invoice_number}</h1>
<div class="meta">
  Status: <span class="status status-{status}">{status}</span>
  &middot; Ausgestellt: {issued_at_human}
  &middot; Fällig: {due_at_human}
</div>
<div class="addr">{billing_address_block}</div>
<table>
  <thead><tr><th>Position</th><th class="num">Menge</th><th class="num">Einzelpreis</th><th class="num">Summe</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<div class="totals">
  Zwischensumme: € {subtotal_eur}<br>
  USt {tax_rate}%: € {tax_eur}<br>
  <span class="grand">Gesamt: € {total_eur}</span>
</div>
{notes_block}
</body></html>"""


def _render_html(inv: Dict[str, Any]) -> str:
    addr = inv.get("billingAddress") or {}
    addr_block = ""
    if addr:
        addr_block = "\n".join(filter(None, [
            addr.get("name", ""),
            addr.get("street", ""),
            f"{addr.get('postcode','')} {addr.get('city','')}".strip(),
            addr.get("country", ""),
            f"USt-ID: {addr['vatId']}" if addr.get("vatId") else "",
        ]))
    rows_html = "".join(
        f"<tr><td>{li.get('description','')}</td>"
        f"<td class='num'>{li.get('quantity','')}</td>"
        f"<td class='num'>{li.get('unitPriceEur',0):.2f}</td>"
        f"<td class='num'>{li.get('totalEur',0):.2f}</td></tr>"
        for li in (inv.get("lineItems") or [])
    )
    notes_block = f"<div class='notes'>{inv['notes']}</div>" if inv.get("notes") else ""
    def _human(iso: Optional[str]) -> str:
        return iso[:10] if iso else "—"
    return _HTML_TEMPLATE.format(
        invoice_number=inv["invoiceNumber"],
        status=inv["status"],
        issued_at_human=_human(inv.get("issuedAt")),
        due_at_human=_human(inv.get("dueAt")),
        billing_address_block=addr_block or "—",
        rows_html=rows_html,
        subtotal_eur=inv["subtotalEur"],
        tax_rate=inv["taxRate"],
        tax_eur=inv["taxEur"],
        total_eur=inv["totalEur"],
        notes_block=notes_block,
    )


@router.get("/{invoice_id}/html", response_class=Response)
async def render_invoice_html(
    invoice_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Response:
    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1", uuid.UUID(invoice_id))
    if not r:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    if not claims.is_admin and str(r["user_id"]) != claims.user_id:
        raise HTTPException(status_code=403, detail="Forbidden: not your invoice")
    html = _render_html(_row(r))
    return Response(content=html, media_type="text/html")


# ---------------------------------------------------------------------------
# Send via Resend — email invoice HTML to the customer.
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/send")
async def send_invoice(
    invoice_id: str,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Send the invoice as an email (HTML body) via Resend.

    Pulls recipient from the linked user's email. Marks the invoice's
    sent_at and writes a billing_events trail. Fail-fast when RESEND_API_KEY
    or sender email is not configured — no silent skip for outbound
    customer email.
    """
    import os
    import json as _json
    import httpx as _httpx

    resend_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_INVOICE_SENDER", "billing@werking.tools")
    if not resend_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY not configured on bridge")

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT i.*, u.email AS user_email, u.name AS user_name FROM invoices i "
            "JOIN users u ON u.id = i.user_id WHERE i.id = $1",
            uuid.UUID(invoice_id),
        )
    if not r:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    recipient = r["user_email"]
    if not recipient:
        raise HTTPException(status_code=409, detail="Invoice user has no email — cannot send")

    invoice_dict = _row(r)
    html_body = _render_html(invoice_dict)
    subject = f"Rechnung {invoice_dict['invoiceNumber']} — Werkingflow"

    async with _httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": html_body,
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Resend error {resp.status_code}: {resp.text[:200]}")
    resend_payload = resp.json()
    resend_id = resend_payload.get("id")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE invoices SET sent_at = COALESCE(sent_at, NOW()), status = CASE WHEN status = 'draft' THEN 'issued'::invoice_status ELSE status END, updated_at = NOW() WHERE id = $1",
                uuid.UUID(invoice_id),
            )
            await conn.execute(
                """
                INSERT INTO billing_events
                  (event_type, user_id, invoice_id, amount_eur, source, payload)
                VALUES ('invoice.sent', $1, $2, $3, 'admin', $4::jsonb)
                """,
                r["user_id"],
                uuid.UUID(invoice_id),
                r["total_eur"],
                _json.dumps({"recipient": recipient, "resendId": resend_id, "subject": subject}),
            )
    return {
        "invoiceId": invoice_id,
        "recipient": recipient,
        "resendId": resend_id,
        "sentAt": "now",
    }

