"""Service principals — per-caller Bridge identities (Stufe 2).

A principal is a DB-backed caller identity (see migration 034) with its own
token, an app allowlist, an optional path allowlist, and a monthly EUR cap for
calls it makes without an end-user. This module owns:

  * token → principal resolution (the hot path, cached briefly), and
  * the CRUD/rotate helpers the admin route uses.

Rollout is gated by BRIDGE_PRINCIPALS_ENABLED (default OFF) — see auth.py. While
OFF nothing here is consulted. While ON, the legacy AI_BRIDGE_API_KEY resolves
to the synthetic LEGACY_PRINCIPAL below (allowed_apps '*') so the migration can
move callers one at a time without a flag day.

Fail-loud: a DB error during resolution is NOT swallowed into "no principal"
(that would silently reopen the door). The caller (auth.py) decides how to fail
based on the flag, but this module never turns an error into a permissive answer.

Resolution path (ADR-0009 Schritt 2a, C2): a cache miss goes to platform-api
first (GET /v1/internal/principals/{token_hash}, see src/internal_routes.py)
instead of a direct Postgres query. If platform-api is unreachable, resolution
falls back to the direct DB query IN THE SAME CALL (one logger.error, no
silent pass-through) — that fallback stays available as long as this worker
still has BRIDGE_DB_URL set (it does today; it will not once a worker actually
moves off production-barrier, ADR-0009 Schritt 3/4). If NEITHER channel can
answer, resolution raises rather than returning None — the "fail-loud" promise
above must survive the platform-api hop, not just the direct-DB path it used
to be.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from src.db.client import get_pool, is_db_enabled
from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)

# Wildcard entry meaning "any" in allowed_apps / allowed_paths.
WILDCARD = "*"


def hash_token(token: str) -> str:
    """sha256 hex of a cleartext token. The only form ever compared/stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: Optional[str]
    name: str
    allowed_apps: List[str] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=lambda: [WILDCARD])
    monthly_cap_eur: Optional[float] = None
    # True for the synthetic legacy principal (not a DB row) — lets callers and
    # metrics distinguish "still on the shared key" from a real per-caller token.
    is_legacy: bool = False

    def may_use_app(self, app_id: Optional[str]) -> bool:
        if WILDCARD in self.allowed_apps:
            return True
        if not app_id:
            # No X-App-ID and not a wildcard principal → cannot claim any app.
            return False
        return app_id in self.allowed_apps

    def may_use_path(self, path: Optional[str]) -> bool:
        if not self.allowed_paths or WILDCARD in self.allowed_paths:
            return True
        if path is None:
            return False
        if path in self.allowed_paths:
            return True
        return any(_segment_match(entry, path) for entry in self.allowed_paths)


# Ein-Segment-Eintrag in allowed_paths: "/v1/jobs/*" erlaubt "/v1/jobs/<id>",
# aber weder "/v1/jobs" noch "/v1/jobs/<id>/<mehr>". Pfade mit ID (Job-Poll,
# DELETE /v1/jobs/{id}) waren bisher nur ueber den Voll-Joker "*" freigebbar.
# Kein Praefix ueber mehrere Segmente: ein Eintrag soll nie still Routen
# freigeben, die erst spaeter unter demselben Praefix entstehen.
SEGMENT_SUFFIX = "/" + WILDCARD


def _segment_match(entry: str, path: str) -> bool:
    if not entry.endswith(SEGMENT_SUFFIX) or len(entry) <= len(SEGMENT_SUFFIX):
        return False
    stem = entry[: -len(WILDCARD)]  # "/v1/jobs/"
    if not path.startswith(stem):
        return False
    segment = path[len(stem):]
    # Genau ein nicht leeres Segment; "." und ".." sind keine IDs.
    return bool(segment) and "/" not in segment and segment not in (".", "..")


# Synthetic identity the shared AI_BRIDGE_API_KEY maps to while callers are being
# migrated onto their own tokens. Not a DB row; allowed everywhere so enabling
# the flag never breaks an un-migrated caller. Removed by deactivating it in
# code once every caller has a real principal (flip via config, see spec Stufe 2).
LEGACY_PRINCIPAL = Principal(
    id=None,
    name="legacy",
    allowed_apps=[WILDCARD],
    allowed_paths=[WILDCARD],
    monthly_cap_eur=None,
    is_legacy=True,
)


# ── Resolution cache ────────────────────────────────────────────────────────
# Every request resolves a token → principal. Cache token_hash → (principal|None)
# for a short TTL so a burst doesn't hit Postgres per call, but a deactivation
# still propagates within seconds. A miss (unknown token) is cached too, so a
# brute-force/scanner flood doesn't become a DB amplification.
_CACHE_TTL_S = float(os.getenv("BRIDGE_PRINCIPALS_CACHE_TTL_S", "20"))
_cache: dict[str, tuple[float, Optional[Principal]]] = {}


def _cache_get(token_hash: str) -> tuple[bool, Optional[Principal]]:
    entry = _cache.get(token_hash)
    if entry is None:
        return False, None
    ts, principal = entry
    if (time.monotonic() - ts) > _CACHE_TTL_S:
        _cache.pop(token_hash, None)
        return False, None
    return True, principal


def _cache_put(token_hash: str, principal: Optional[Principal]) -> None:
    _cache[token_hash] = (time.monotonic(), principal)


def invalidate_cache() -> None:
    """Drop all cached resolutions — called after any create/rotate/deactivate so
    a token change takes effect immediately instead of after the TTL."""
    _cache.clear()


def _row_to_principal(row) -> Principal:
    return Principal(
        id=str(row["id"]),
        name=row["name"],
        allowed_apps=list(row["allowed_apps"] or []),
        allowed_paths=list(row["allowed_paths"] or [WILDCARD]),
        monthly_cap_eur=float(row["monthly_cap_eur"]) if row["monthly_cap_eur"] is not None else None,
    )


def _dict_to_principal(data: dict) -> Principal:
    """Same shape as _row_to_principal, but from platform-api's JSON body
    instead of an asyncpg row — both are the columns of one service_principals
    row, so the field set matches exactly."""
    return Principal(
        id=str(data["id"]),
        name=data["name"],
        allowed_apps=list(data.get("allowed_apps") or []),
        allowed_paths=list(data.get("allowed_paths") or [WILDCARD]),
        monthly_cap_eur=float(data["monthly_cap_eur"]) if data.get("monthly_cap_eur") is not None else None,
    )


async def get_principal_row_by_hash(token_hash: str) -> Optional[dict]:
    """Pure DB read by an already-hashed token — the one query both platform-api's
    GET /v1/internal/principals/{token_hash} (src/internal_routes.py) and this
    module's direct-DB fallback run. Returns None on no active match. Raises on
    DB error (callers decide how to fail)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, allowed_apps, allowed_paths, monthly_cap_eur
            FROM service_principals
            WHERE token_hash = $1 AND active AND cardinality(proxy_routes) = 0
            """,
            token_hash,
        )
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "allowed_apps": list(row["allowed_apps"] or []),
        "allowed_paths": list(row["allowed_paths"] or [WILDCARD]),
        "monthly_cap_eur": float(row["monthly_cap_eur"]) if row["monthly_cap_eur"] is not None else None,
    }


async def _resolve_via_direct_db(token_hash: str) -> Optional[Principal]:
    """The pre-Schritt-2a path, now the fallback. Raises loud (not a silent
    None) when there is no DB to fall back to — that means platform-api AND the
    direct connection both failed, which is the case this module's fail-loud
    contract exists for."""
    if not is_db_enabled():
        raise RuntimeError(
            "principal resolution: platform-api was unreachable and no direct-DB "
            "fallback is configured (BRIDGE_DB_URL unset). Cannot resolve. If this "
            "worker has genuinely moved off production-barrier (ADR-0009 Schritt "
            "3/4), the direct-DB fallback is expected to be gone and platform-api "
            "reachability is now load-bearing — investigate why it failed."
        )
    row = await get_principal_row_by_hash(token_hash)
    return _dict_to_principal(row) if row else None


async def resolve_principal_by_token(token: str) -> Optional[Principal]:
    """Resolve a cleartext token to an active Principal, or None if no active
    principal has that token. Cached briefly.

    ADR-0009 Schritt 2a: the cache miss goes to platform-api first; a direct-DB
    read only runs when platform-api could not answer (see module docstring).
    Raises on total failure (never masks it as None)."""
    token_hash = hash_token(token)
    hit, cached = _cache_get(token_hash)
    if hit:
        return cached

    try:
        resp = await call_platform("GET", f"/v1/internal/principals/{token_hash}")
    except PlatformUnavailable as e:
        logger.error(
            "principal lookup via platform-api failed (%s) — falling back to direct DB", e
        )
        principal = await _resolve_via_direct_db(token_hash)
    else:
        if resp.status_code == 200 and isinstance(resp.json, dict) and "principal" in resp.json:
            raw = resp.json["principal"]
            principal = _dict_to_principal(raw) if raw else None
        else:
            # An unexpected contract (wrong status, malformed body) is treated
            # the same as unreachable: fall back rather than silently answer
            # "no principal" for what might be a platform-api bug.
            logger.error(
                "principal lookup via platform-api returned unexpected "
                "status=%s body=%r — falling back to direct DB",
                resp.status_code, resp.json,
            )
            principal = await _resolve_via_direct_db(token_hash)

    _cache_put(token_hash, principal)
    return principal


# ── Admin CRUD (used by src/principals_routes.py) ───────────────────────────

async def create_principal(
    name: str,
    token: str,
    allowed_apps: List[str],
    allowed_paths: Optional[List[str]] = None,
    monthly_cap_eur: Optional[float] = None,
) -> Principal:
    """Insert a new principal. The cleartext token is provided by the route (it
    generates + returns it once); here we store only the hash + prefix."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO service_principals
                (name, token_hash, token_prefix, allowed_apps, allowed_paths, monthly_cap_eur)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, name, allowed_apps, allowed_paths, monthly_cap_eur
            """,
            name,
            hash_token(token),
            token[:8],
            allowed_apps,
            allowed_paths if allowed_paths is not None else [WILDCARD],
            monthly_cap_eur,
        )
    invalidate_cache()
    return _row_to_principal(row)


async def rotate_principal_token(name: str, new_token: str) -> bool:
    """Replace a principal's token. Returns False if no such principal."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE service_principals
            SET token_hash = $2, token_prefix = $3, rotated_at = NOW()
            WHERE name = $1
            """,
            name,
            hash_token(new_token),
            new_token[:8],
        )
    invalidate_cache()
    return result.endswith("1")


async def set_principal_active(name: str, active: bool) -> bool:
    """Activate/deactivate a principal. A deactivated principal's token is dead
    on the next resolution (cache invalidated here). Returns False if not found."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE service_principals SET active = $2 WHERE name = $1",
            name,
            active,
        )
    invalidate_cache()
    return result.endswith("1")


async def list_principals() -> List[dict]:
    """All principals (no token material) for the admin view."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, token_prefix, allowed_apps, allowed_paths,
                   monthly_cap_eur, active, rotated_at, created_at
            FROM service_principals
            ORDER BY name
            """
        )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "token_prefix": r["token_prefix"],
            "allowed_apps": list(r["allowed_apps"] or []),
            "allowed_paths": list(r["allowed_paths"] or []),
            "monthly_cap_eur": float(r["monthly_cap_eur"]) if r["monthly_cap_eur"] is not None else None,
            "active": r["active"],
            "rotated_at": r["rotated_at"].isoformat() if r["rotated_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── Gebundene Tokens (ProxyV2 + Sandbox-Lease A, 17.11.2026) ─────────────────
# Ein gebundener Principal ist KEIN KI-Principal: get_principal_row_by_hash oben
# schliesst ihn aus (cardinality(proxy_routes) = 0); auf verify_api_key-Wegen ist
# sein Token also 401. Er gilt nur ueber den strikten Verifier in
# src/api_auth/deps.py (Token-Praefix BOUND_TOKEN_PREFIX waehlt den Verifier, das
# Praefix selbst authentisiert nichts) fuer GENAU EINE App, EINEN Nutzer
# (proxy_user_id) in EINEM Mandanten (proxy_tenant_id, serverseitige tenants.id)
# bis proxy_expires_at, nur auf den Routen in proxy_routes ("METHODE /vorlage").
# Die Code-Obergrenze BOUND_ROUTEN_OBERGRENZE deckelt, was die DB je freigeben kann.
BOUND_TOKEN_PREFIX = "bpx_"
BOUND_ROUTEN_OBERGRENZE = frozenset({
    # ProxyV2 (Works-App): Abo-Gate + eigener Aktivitaetsverlauf
    "GET /v1/billing/{user_id}/subscriptions",
    "GET /v1/activity/query",
    # Sandbox-Lease A (Daemon): eigene Lease, eigener Verbrauch, eigenes Budget, eigene Gespraeche
    "POST /v1/sandbox/lease-token",
    "POST /v1/sandbox/lease-token/{lease_id}/heartbeat",
    "POST /v1/sandbox/lease-token/{lease_id}/release",
    "POST /v1/sandbox/lease-token/{lease_id}/attach-session",
    "POST /v1/sandbox/usage/record",
    "POST /v1/budget/check",
    "POST /v1/budget/deduct",
    "GET /v1/sandbox/conversations",
    "PUT /v1/sandbox/conversations/{conversation_id}",
    "PUT /v1/sandbox/conversations/{conversation_id}/transcript",
    "GET /v1/sandbox/conversations/{conversation_id}/transcript",
    # Identitaet: NUR die eigene E-Mail (Verifier erzwingt email == gebundene Mail)
    "GET /v1/admin/users/lookup",
})
#: allowed_paths eines gebundenen Principals: passt auf keinen KI-Pfad (kein "*", nicht leer).
BOUND_KEIN_KI_PFAD = "(gebunden)"


@dataclass(frozen=True)
class BoundPrincipal:
    id: str
    name: str
    app_id: str
    routes: frozenset
    user_id: str
    user_email: Optional[str]
    tenant_id: str
    user_tenant_id: Optional[str]


async def get_bound_principal_by_token(token: str) -> Optional[BoundPrincipal]:
    """Aktiver, nicht abgelaufener gebundener Principal zum Token, sonst None.
    Kein Cache: Widerruf (active=false) und Ablauf wirken sofort. Mandant und
    E-Mail des Nutzers werden bei JEDEM Aufruf aus users gelesen. DB-Fehler
    werden nicht verschluckt (fail-loud -> 5xx, nie Operator)."""
    if not is_db_enabled():
        raise RuntimeError("bound token verification needs the DB")
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT sp.id, sp.name, sp.allowed_apps, sp.proxy_routes, sp.proxy_user_id,
                   sp.proxy_tenant_id, u.tenant_id AS user_tenant_id, u.email AS user_email
              FROM service_principals sp
              LEFT JOIN users u ON u.id = sp.proxy_user_id
             WHERE sp.token_hash = $1 AND sp.active
               AND cardinality(sp.proxy_routes) > 0
               AND sp.proxy_expires_at IS NOT NULL AND sp.proxy_expires_at > NOW()
            """,
            hash_token(token),
        )
    if row is None or row["proxy_user_id"] is None or not row["proxy_tenant_id"]:
        return None
    apps = list(row["allowed_apps"] or [])
    if len(apps) != 1 or apps[0] == WILDCARD:
        return None
    return BoundPrincipal(
        id=str(row["id"]), name=row["name"], app_id=apps[0],
        routes=frozenset(row["proxy_routes"] or []) & BOUND_ROUTEN_OBERGRENZE,
        user_id=str(row["proxy_user_id"]), user_email=row["user_email"],
        tenant_id=row["proxy_tenant_id"], user_tenant_id=row["user_tenant_id"],
    )


async def bound_owns_lease(bp: BoundPrincipal, lease_id: str) -> bool:
    """Gehoert die Lease genau diesem Nutzer, Mandanten und dieser App?"""
    import uuid as _uuid
    try:
        lid = _uuid.UUID(str(lease_id))
    except ValueError:
        return False
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, tenant_id, app FROM sandbox_leases WHERE lease_id = $1", lid)
    return bool(row) and str(row["user_id"]) == bp.user_id and row["tenant_id"] == bp.tenant_id \
        and row["app"] == bp.app_id


async def bound_owns_conversation(bp: BoundPrincipal, conversation_id: str) -> Optional[bool]:
    """True = eigenes Gespraech, False = fremdes, None = existiert (noch) nicht."""
    if not conversation_id:
        return False
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, tenant_id, app FROM sandbox_conversations WHERE id = $1",
            str(conversation_id))
    if row is None:
        return None
    return str(row["user_id"]) == bp.user_id and row["tenant_id"] == bp.tenant_id \
        and row["app"] == bp.app_id


def validate_bound_spec(routes: List[str], user_id: str, tenant_id: str,
                        allowed_apps: List[str]) -> None:
    """Anlegeregeln (fail-closed, ValueError): genau eine App ohne '*', Routen nicht
    leer und Teilmenge der Code-Obergrenze, Nutzer- und Mandanten-ID gesetzt."""
    if len(allowed_apps) != 1 or allowed_apps[0] == WILDCARD:
        raise ValueError("bound principal needs exactly one app, no '*'")
    if not routes or any(r not in BOUND_ROUTEN_OBERGRENZE for r in routes):
        raise ValueError(f"bound routes must be a non-empty subset of {sorted(BOUND_ROUTEN_OBERGRENZE)}")
    if not user_id or not tenant_id:
        raise ValueError("bound principal needs proxy_user_id and proxy_tenant_id")


def new_bound_token() -> str:
    import secrets as _secrets
    return BOUND_TOKEN_PREFIX + _secrets.token_urlsafe(32)


async def create_bound_principal(name: str, token: str, app_id: str, routes: List[str],
                                 user_id: str, tenant_id: str, expires_at) -> dict:
    """Legt einen gebundenen Principal an. Prueft in DERSELBEN Transaktion, dass
    der Nutzer existiert und serverseitig zu tenant_id gehoert (keine Slug-Annahme)."""
    import uuid as _uuid
    validate_bound_spec(routes, user_id, tenant_id, [app_id])
    if not token.startswith(BOUND_TOKEN_PREFIX):
        raise ValueError("bound token must carry the bound prefix")
    uid = _uuid.UUID(user_id)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchval("SELECT tenant_id FROM users WHERE id = $1", uid)
            if t is None or t != tenant_id:
                raise ValueError("bound user does not exist or does not belong to proxy_tenant_id")
            row = await conn.fetchrow(
                """
                INSERT INTO service_principals
                    (name, token_hash, token_prefix, allowed_apps, allowed_paths,
                     proxy_routes, proxy_user_id, proxy_tenant_id, proxy_expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, name, allowed_apps, proxy_routes, proxy_user_id,
                          proxy_tenant_id, proxy_expires_at
                """,
                name, hash_token(token), token[:8], [app_id], [BOUND_KEIN_KI_PFAD],
                sorted(routes), uid, tenant_id, expires_at,
            )
    invalidate_cache()
    return {"id": str(row["id"]), "name": row["name"], "allowed_apps": list(row["allowed_apps"]),
            "proxy_routes": list(row["proxy_routes"]), "proxy_user_id": str(row["proxy_user_id"]),
            "proxy_tenant_id": row["proxy_tenant_id"], "proxy_expires_at": row["proxy_expires_at"].isoformat()}
