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
# HTML rendering — professional invoice layout (Engelmann ZT e.U.)
#
# Issuer info is env-driven so we can change company data, IBAN, BIC etc.
# without redeploying code. Defaults match the Engelmann Data Energyneering
# ZT e.U. business setup.
# ---------------------------------------------------------------------------

import os as _os


def _issuer() -> Dict[str, str]:
    return {
        "name":     _os.environ.get("INVOICE_ISSUER_NAME",     "Engelmann Data Energyneering ZT e.U."),
        "street":   _os.environ.get("INVOICE_ISSUER_STREET",   "Florianigasse 17/19"),
        "city":     _os.environ.get("INVOICE_ISSUER_CITY",     "1080 Wien"),
        "country":  _os.environ.get("INVOICE_ISSUER_COUNTRY",  "Österreich"),
        "phone":    _os.environ.get("INVOICE_ISSUER_PHONE",    "+43 676 542 3883"),
        "email":    _os.environ.get("INVOICE_ISSUER_EMAIL",    "office@data-energyneering.at"),
        "web":      _os.environ.get("INVOICE_ISSUER_WEB",      "www.werkingflow.at"),
        "vatId":    _os.environ.get("INVOICE_ISSUER_VAT_ID",   "ATU78156638"),
        "taxNr":    _os.environ.get("INVOICE_ISSUER_TAX_NR",   "06 289/4969"),
        "regCourt": _os.environ.get("INVOICE_ISSUER_REG_COURT","Landesgericht Wien"),
        "iban":     _os.environ.get("INVOICE_ISSUER_IBAN",     ""),
        "bic":      _os.environ.get("INVOICE_ISSUER_BIC",      ""),
        "bankName": _os.environ.get("INVOICE_ISSUER_BANK_NAME",""),
        "ownerName":_os.environ.get("INVOICE_ISSUER_OWNER",    "Dipl.-Ing. Dr. Rafael Engelmann"),
    }


def _human_date(iso: Optional[str]) -> str:
    """Format ISO datetime as DD.MM.YYYY (AT/DE convention)."""
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%d.%m.%Y")
    except Exception:
        return iso[:10] if len(iso) >= 10 else iso


_HTML_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>Rechnung {invoice_number}</title>
<style>
  @page {{ size: A4; margin: 18mm 14mm 22mm 14mm; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    color: #1f2933;
    font-size: 12px;
    line-height: 1.45;
    background: #fff;
  }}
  .page {{ max-width: 800px; margin: 0 auto; padding: 32px 40px 48px; }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    border-bottom: 2px solid #1f2933;
    padding-bottom: 18px;
    margin-bottom: 28px;
  }}
  .brand .logo {{ font-size: 22px; font-weight: 700; color: #1f2933; letter-spacing: -0.5px; }}
  .brand .tag {{ color: #6b7280; font-size: 11px; margin-top: 4px; }}
  .issuer {{ text-align: right; font-size: 11px; color: #374151; line-height: 1.5; }}
  .issuer .name {{ font-weight: 600; color: #111827; }}
  .row {{ display: flex; gap: 40px; margin-bottom: 28px; }}
  .row > div {{ flex: 1; }}
  .label {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
    color: #6b7280; margin-bottom: 6px;
  }}
  .addr {{ white-space: pre-line; font-size: 12px; color: #1f2933; }}
  .meta-table {{ width: 100%; font-size: 12px; }}
  .meta-table td {{ padding: 2px 0; vertical-align: top; }}
  .meta-table td.k {{ color: #6b7280; padding-right: 14px; white-space: nowrap; }}
  h1 {{ font-size: 28px; margin: 0 0 4px; color: #1f2933; font-weight: 700; letter-spacing: -0.5px; }}
  .status {{
    display: inline-block; padding: 3px 10px; border-radius: 4px;
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em;
  }}
  .status-paid {{ background: #d1fae5; color: #065f46; }}
  .status-issued {{ background: #dbeafe; color: #1e3a8a; }}
  .status-draft {{ background: #e5e7eb; color: #374151; }}
  .status-cancelled, .status-refunded {{ background: #fee2e2; color: #991b1b; }}
  table.items {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 12px; }}
  table.items thead th {{
    background: #f3f4f6; color: #374151; font-weight: 600; text-align: left;
    padding: 10px 8px; border-bottom: 2px solid #1f2933;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  }}
  table.items tbody td {{ padding: 10px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
  table.items td.num, table.items th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  table.items td.desc {{ width: 60%; color: #1f2933; }}
  .totals-wrap {{ display: flex; justify-content: flex-end; margin-top: 8px; }}
  .totals {{ min-width: 280px; font-size: 12px; }}
  .totals .line {{ display: flex; justify-content: space-between; padding: 4px 0; }}
  .totals .line .k {{ color: #6b7280; }}
  .totals .line .v {{ font-variant-numeric: tabular-nums; }}
  .totals .grand {{
    border-top: 2px solid #1f2933; margin-top: 6px; padding-top: 8px;
    font-size: 16px; font-weight: 700; color: #1f2933;
  }}
  .payment {{
    margin: 32px 0 24px; padding: 16px 18px;
    background: #f9fafb; border-left: 3px solid #1f2933; font-size: 12px;
  }}
  .payment.paid {{ background: #d1fae5; border-color: #065f46; color: #065f46; }}
  .payment h3 {{ margin: 0 0 8px; font-size: 13px; font-weight: 600; }}
  .payment table {{ width: 100%; }}
  .payment td.k {{ color: #6b7280; padding-right: 12px; padding-bottom: 2px; white-space: nowrap; }}
  .payment td.v {{ font-variant-numeric: tabular-nums; }}
  .notes {{
    margin: 16px 0; padding: 12px 14px;
    background: #fffbeb; border-left: 3px solid #d97706;
    font-size: 11px; color: #78350f;
  }}
  .footer {{
    margin-top: 40px; padding-top: 16px; border-top: 1px solid #e5e7eb;
    font-size: 10px; color: #6b7280; line-height: 1.5;
  }}
  .footer .grid {{ display: flex; gap: 30px; }}
  .footer .grid > div {{ flex: 1; }}
  .footer strong {{ color: #374151; font-weight: 600; }}
  @media print {{
    body {{ font-size: 11px; }}
    .page {{ padding: 0; max-width: none; }}
  }}
</style></head><body>
<div class="page">

<div class="header">
  <div class="brand">
    <div class="logo">{issuer_name}</div>
    <div class="tag">Werkingflow — Engineering-Automatisierung mit KI</div>
  </div>
  <div class="issuer">
    <div class="name">{issuer_name}</div>
    <div>{issuer_street}</div>
    <div>{issuer_city}</div>
    <div>{issuer_country}</div>
    <div style="margin-top:6px;">Tel: {issuer_phone}</div>
    <div>{issuer_email}</div>
    <div>{issuer_web}</div>
  </div>
</div>

<div class="row">
  <div>
    <div class="label">Rechnung an</div>
    <div class="addr">{billing_address_block}</div>
  </div>
  <div>
    <h1>Rechnung</h1>
    <div style="margin-bottom:10px;"><span class="status status-{status}">{status}</span></div>
    <table class="meta-table">
      <tr><td class="k">Rechnungs-Nr.</td><td><strong>{invoice_number}</strong></td></tr>
      <tr><td class="k">Ausgestellt am</td><td>{issued_at_human}</td></tr>
      <tr><td class="k">Fällig am</td><td>{due_at_human}</td></tr>
      <tr><td class="k">Bezahlt am</td><td>{paid_at_human}</td></tr>
    </table>
  </div>
</div>

<table class="items">
  <thead>
    <tr>
      <th class="desc">Position</th>
      <th class="num">Menge</th>
      <th class="num">Einzelpreis</th>
      <th class="num">Summe</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>

<div class="totals-wrap"><div class="totals">
  <div class="line"><span class="k">Zwischensumme (netto)</span><span class="v">€ {subtotal_eur}</span></div>
  <div class="line"><span class="k">USt {tax_rate}%</span><span class="v">€ {tax_eur}</span></div>
  <div class="line grand"><span class="k">Gesamtbetrag</span><span class="v">€ {total_eur}</span></div>
</div></div>

{payment_block}

{notes_block}

<div class="footer">
  <div class="grid">
    <div>
      <strong>{issuer_name}</strong><br>
      {issuer_owner}<br>
      {issuer_street}<br>
      {issuer_city}, {issuer_country}
    </div>
    <div>
      <strong>Kontakt</strong><br>
      Tel: {issuer_phone}<br>
      {issuer_email}<br>
      {issuer_web}
    </div>
    <div>
      <strong>Steuer &amp; Recht</strong><br>
      UID: {issuer_vat_id}<br>
      Steuer-Nr.: {issuer_tax_nr}<br>
      {issuer_reg_court}
    </div>
  </div>
</div>

</div>
</body></html>"""


def _render_html(inv: Dict[str, Any]) -> str:
    issuer = _issuer()
    addr = inv.get("billingAddress") or {}
    addr_lines = []
    if addr:
        if addr.get("name"):    addr_lines.append(addr["name"])
        if addr.get("street"):  addr_lines.append(addr["street"])
        city_line = f"{addr.get('postcode','')} {addr.get('city','')}".strip()
        if city_line:           addr_lines.append(city_line)
        if addr.get("country"): addr_lines.append(addr["country"])
        if addr.get("vatId"):   addr_lines.append(f"USt-ID: {addr['vatId']}")
    addr_block = "\n".join(addr_lines) if addr_lines else "—"

    rows_html = "".join(
        f"<tr><td class='desc'>{li.get('description','')}</td>"
        f"<td class='num'>{li.get('quantity','')}</td>"
        f"<td class='num'>{float(li.get('unitPriceEur',0)):.2f}</td>"
        f"<td class='num'>{float(li.get('totalEur',0)):.2f}</td></tr>"
        for li in (inv.get("lineItems") or [])
    ) or "<tr><td colspan='4' style='color:#9ca3af;text-align:center;padding:18px;'>Keine Positionen</td></tr>"

    notes_block = f"<div class='notes'>{inv['notes']}</div>" if inv.get("notes") else ""

    status = inv.get("status", "draft")
    if status == "paid":
        payment_block = (
            "<div class='payment paid'>"
            "<h3>✓ Bezahlt</h3>"
            f"Diese Rechnung wurde am {_human_date(inv.get('paidAt'))} beglichen. "
            "Vielen Dank für Ihre Zahlung."
            "</div>"
        )
    elif issuer["iban"]:
        payment_block = (
            "<div class='payment'>"
            "<h3>Zahlungsinformationen</h3>"
            "<table>"
            f"<tr><td class='k'>Empfänger</td><td class='v'>{issuer['name']}</td></tr>"
            f"<tr><td class='k'>IBAN</td><td class='v'>{issuer['iban']}</td></tr>"
            f"<tr><td class='k'>BIC</td><td class='v'>{issuer['bic'] or '—'}</td></tr>"
            f"<tr><td class='k'>Bank</td><td class='v'>{issuer['bankName'] or '—'}</td></tr>"
            f"<tr><td class='k'>Verwendungszweck</td><td class='v'><strong>{inv.get('invoiceNumber','')}</strong></td></tr>"
            "</table>"
            "</div>"
        )
    else:
        payment_block = (
            "<div class='payment'>"
            "<h3>Zahlungsinformationen</h3>"
            f"Bitte überweisen Sie den Gesamtbetrag bis {_human_date(inv.get('dueAt'))} "
            f"unter Angabe der Rechnungsnummer <strong>{inv.get('invoiceNumber','')}</strong>."
            "</div>"
        )

    return _HTML_TEMPLATE.format(
        invoice_number=inv["invoiceNumber"],
        status=status,
        issued_at_human=_human_date(inv.get("issuedAt")),
        due_at_human=_human_date(inv.get("dueAt")),
        paid_at_human=_human_date(inv.get("paidAt")),
        billing_address_block=addr_block,
        rows_html=rows_html,
        subtotal_eur=inv["subtotalEur"],
        tax_rate=inv["taxRate"],
        tax_eur=inv["taxEur"],
        total_eur=inv["totalEur"],
        payment_block=payment_block,
        notes_block=notes_block,
        issuer_name=issuer["name"],
        issuer_street=issuer["street"],
        issuer_city=issuer["city"],
        issuer_country=issuer["country"],
        issuer_phone=issuer["phone"],
        issuer_email=issuer["email"],
        issuer_web=issuer["web"],
        issuer_owner=issuer["ownerName"],
        issuer_vat_id=issuer["vatId"],
        issuer_tax_nr=issuer["taxNr"],
        issuer_reg_court=issuer["regCourt"],
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

