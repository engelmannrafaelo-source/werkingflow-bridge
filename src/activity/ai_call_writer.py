"""
AI-call activity writer — the Bridge logs every LLM call itself.

Why this exists (ADR 0007 / token-tracking consolidation):
  Apps used to be responsible for POSTing /v1/activity/log after each AI call.
  That is unenforceable — any code path that calls the Bridge directly and
  forgets to log creates a silent gap in usage tracking. werking-report had
  13 such gaps.

  The Bridge sees *every* LLM call (it is the proxy). So the Bridge logs it,
  exactly once, right where it already records in-memory prompt-metrics. Apps
  no longer log anything — they only send attribution headers (X-App-ID,
  X-User-ID), which a Layer-0 validator enforces.

Where the rows are written (ADR-0009 Schritt 2c):
  This module holds NO database connection. It decides everything about the
  call — cost, provider vocabulary, billing mode, which pot pays, every skip
  branch — and then states the result over HTTP to platform-api
  (src/activity/ledger_client.py → src/activity/ledger_db.py). The split is
  "data, not verdicts": the pure, unit-tested functions stayed here, only the
  statements that need a connection moved.

  That is what lets a worker run without BRIDGE_DB_URL and therefore live on a
  different host than the customer database. It costs nothing in durability,
  because the write-ahead spool below already covers a write that does not
  arrive — and a lost HTTP answer is the same thing as a lost DB answer, with
  the same remedy: the row stays owed and is replayed.

Contract:
  • Fire-and-forget. A tracking failure must NEVER break the user-facing
    call — every error is swallowed with a warning.
  • tenant_id is resolved from the user (X-User-ID → users.tenant_id).
  • A call with no resolvable tenant (no user, internal Bridge job) is NOT
    written to `activities` — that table is tenant-scoped and NOT NULL.
    Such calls still land in in-memory prompt-metrics. App calls always
    carry X-User-ID (Layer-0 enforced), so in practice every app call is
    persisted.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from src.activity import ledger_client, ledger_spool
from src.activity.ledger_spool import (
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_WRITTEN,
)
from src.activity.app_registry import normalize_app_id
from src.activity.providers import REAL_COST_PROVIDERS, normalize_ledger_provider
from src.attribution import ANONYMOUS_USER_ID
from src.budget.plan_resolution import PlanResolutionError
from src.budget.plans import AmbiguousPlanCatalog
from src.pricing import cost_eur, PRICING_VERSION

logger = logging.getLogger(__name__)

# Incoherent billing configuration/data — distinct from transient infra errors,
# and logged apart from them (see _deduct_call_cost).
_PLAN_RESOLUTION_ERRORS = (AmbiguousPlanCatalog, PlanResolutionError)

# Per-identifier skip counters — tracks how often each non-UUID/unresolvable
# user_id is seen so warnings show frequency, not just isolated occurrences.
_skip_counts: dict = defaultdict(int)

# ANONYMOUS_USER_ID (imported above) is the synthetic identity every EXPLICITLY
# anonymous call books to (fixed UUID, migration 032). Anonymous ≠ missing:
# 'anonymous:<grund>' is a deliberate app statement ("this call-site has no
# logged-in user by design") and gets its own accounting bucket; a missing
# X-User-ID is a leak (counted in src/attribution.py metrics, rejected once
# BRIDGE_ATTRIBUTION_ENFORCE=true). The constant lives in src/attribution.py,
# next to the marker semantics it belongs to, and is re-exported here because
# this is where callers expect to find it.


def resolve_ledger_cost(
    billing_mode_text: str, provider: str, call_cost_eur: float
) -> tuple:
    """Map (tenant billing mode, serving provider) to the usage_events row's
    (billing_mode enum, real_cost_eur).

    real_cost_eur answers "what do WE pay the provider for this call":
    - subscription tenants served by our subscription-covered Anthropic
      accounts have zero marginal cost → 0.0 (per-call cost is hypothetical).
    - Bedrock is pay-per-use to AWS regardless of the tenant's plan — its
      calls ALWAYS carry the real cost, or the 1:1 billing audit reads €0
      while the AWS invoice grows.
    - research-cloud (Weg C, direct Anthropic API key, no subscription
      coverage) is pay-per-use the same way — same reasoning as Bedrock.
    - pay_per_token tenants pay per call → real == hypothetical.

    Pure function — unit-tested in tests/billing/test_ledger_real_cost.py.
    """
    if billing_mode_text == "pay_per_token":
        return "pay_per_token", call_cost_eur
    return "flat_rate_estimated", call_cost_eur if provider in REAL_COST_PROVIDERS else 0.0


async def _deduct_call_cost(
    user_id: str,
    app_id: str,
    cost_eur_amount: float,
    workflow_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    call_ts: Optional[float] = None,
) -> None:
    """
    Post-call budget deduction — best-effort, never raises.

    Runs ONLY after the ledger row was created in this attempt (ADR-0009
    Schritt 1). The ledger row is the authoritative usage record; this
    deduction keeps the user's budget `used_eur` as a running tally so the
    pre-call gate actually has something to gate against. A failure here
    degrades to "no deduction" (the prior behaviour) — it can never break the
    user-facing call.

    Why it is bound to the row rather than running alongside it: this function
    is NOT idempotent. `apply_budget_deduction` is a read-modify-write on
    user_budgets plus a FIFO draw through the TopUp lots, with no dedup key,
    and project_budgets_service.deduct is the same shape. Tied to "the INSERT
    created the row", a replay can never charge twice — the second attempt
    conflicts on idempotency_key, creates nothing, and deducts nothing.

    call_ts: origin time of the call. Only used to make a deduction that
    arrives in a different month than the call VISIBLE (see below) — a silent
    one would make a later invoice dispute unresolvable.

    Routing by plan interval:
    - interval='project' (e.g. Energy): draw from a strictly per-project budget
      keyed by project_id (== workflow_id). It self-provisions on the project's
      first call (lazy allocation — the entitling slot was already consumed by
      the app). Project plans NEVER touch the monthly budget.
    - interval='month' (Report, Engelmann, ...): draw from the monthly tenant
      budget as before.
    """
    try:
        import uuid as _uuid
        from src.budget.plan_resolution import resolve_billing_plan
        from src.budget.routes import (
            apply_budget_deduction_via_platform,
            BudgetDeductionDenied,
        )

        # Both exits below are CORRECT skips, but they are on the budget path:
        # no deduction means the pre-call gate has nothing to gate against, so
        # the reason has to be findable. Same rule as persist_ai_call_activity —
        # a skip that leaves no trace is indistinguishable from a broken writer.
        try:
            uid = _uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            logger.warning(
                "post-call deduction: user=%r is not a UUID (app=%s) — "
                "call NOT metered against any budget", user_id, app_id,
            )
            return

        # Resolved per call, from the entitlement that paid — the deduction has
        # to land in the same pot the gate checked, or the gate guards a tally
        # nobody writes to. See src/budget/plan_resolution.py.
        plan = await resolve_billing_plan(app_id, uid, workflow_id)
        if plan is None:
            logger.debug(
                "post-call deduction: app=%s not in the plan catalog — "
                "not budget-tracked, no deduction for this call", app_id,
            )
            return  # app not in the plan catalog — not budget-tracked

        if plan.interval == "project":
            # Project plans are fully per-project; they never fall through to the
            # monthly budget (a real project customer may have no monthly budget).
            if not workflow_id:
                logger.warning(
                    "post-call deduction: project plan %s call without workflow_id "
                    "for user=%s — cannot attribute to a project budget, skipping",
                    plan.id, user_id,
                )
                return
            from src.billing.project_budgets_service import (
                deduct_via_platform as _deduct_project,
            )

            result = await _deduct_project(
                uid,
                plan.id,
                workflow_id,
                cost_eur_amount,
                allocate_limit_eur=float(plan.api_budget_eur),
                tenant_id=tenant_id,
            )
            if not result.get("exists"):
                logger.warning(
                    "post-call deduction: could not resolve/allocate per-project "
                    "budget for project=%s plan=%s user=%s (tenant_id=%r) — call "
                    "NOT metered against any budget",
                    workflow_id, plan.id, user_id, tenant_id,
                )
            return

        # A deduction that arrives late (spool replay after an outage) draws
        # from the pot that is current NOW, not the one that was current when
        # the call happened. Rare and bounded by the spool's max age — but it
        # must never be silent: the ledger row is recorded in the call's own
        # period while the tally moved, and nobody can reconstruct that from
        # the numbers alone afterwards.
        if call_ts is not None:
            from datetime import datetime as _dt, timezone as _tz

            call_month = _dt.fromtimestamp(call_ts, tz=_tz.utc).strftime("%Y-%m")
            now_month = _dt.now(tz=_tz.utc).strftime("%Y-%m")
            if call_month != now_month:
                logger.error(
                    "post-call deduction CROSSES A MONTH BOUNDARY: call from %s "
                    "is being deducted from the %s budget (user=%s app=%s plan=%s "
                    "%.6f EUR). The usage_events row is recorded in %s — the "
                    "ledger and the running tally disagree about the period for "
                    "this one call. Cause: the row was written late (spool "
                    "replay after a DB outage).",
                    call_month, now_month, user_id, app_id, plan.id,
                    cost_eur_amount, call_month,
                )

        try:
            await apply_budget_deduction_via_platform(uid, plan.id, cost_eur_amount)
        except BudgetDeductionDenied as denied:
            # The call already happened (the gate ran pre-call). A denial
            # here only means the running tally could not fully absorb the
            # cost — the activity row remains the authoritative usage record.
            logger.info(
                "post-call deduction denied (%s) user=%s app=%s",
                denied.reason, user_id, app_id,
            )
    except _PLAN_RESOLUTION_ERRORS:
        # The call already happened, so this cannot fail closed — but an
        # incoherent catalog/allocation is a defect, not a transient miss, and
        # it means this spend lands in NO pot. It must not be filed under the
        # same warning as a DB hiccup.
        logger.exception(
            "post-call deduction: cannot resolve a billing plan for app=%s user=%s "
            "project=%s — this call is NOT metered against any budget",
            app_id, user_id, workflow_id,
        )
    except Exception as e:  # noqa: BLE001 — deduction must never break the call
        logger.warning("post-call budget deduction failed (non-blocking): %s", e)


async def persist_ai_call_activity(
    *,
    app_id: Optional[str],
    user_id: Optional[str],
    agent_id: Optional[str],
    workflow_id: Optional[str],
    model: str,
    input_tokens: int,
    output_tokens: int,
    status: str,
    duration_ms: int,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    app_env: Optional[str] = None,
    provider: str,
    provider_meta: Optional[dict] = None,
    region: Optional[str] = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    search_count: int = 0,
    bridge_origin: Optional[str] = None,
    _call_uid: Optional[str] = None,
    _call_ts: Optional[float] = None,
) -> str:
    """
    Write one ai-call activity row. Never raises for an `Exception` — tracking
    must not break a user-facing call.

    Durability (ADR-0009 Schritt 1). The call's facts are written to a local
    write-ahead spool and fsync'd BEFORE the first database await, and the
    ledger INSERT carries `usage_events.idempotency_key`. Together that means:
    every path out of this function that is not a definitive answer leaves the
    row OWED on disk, and a later replay produces the SAME row rather than a
    second one. What used to be "DB hiccup → ERROR log → unbilled usage" is now
    "DB hiccup → the row arrives late".

    This also closes a loss that had no log at all: `asyncio.CancelledError` is
    a BaseException, so a client disconnect mid-write escaped the `except
    Exception` below and took the row with it, silently. It still propagates
    (swallowing cancellation is its own bug) — but the row is already on disk
    by then, so the flusher writes it.

    Returns one of the ledger_spool outcomes ("written" / "duplicate" /
    "skipped:<reason>" / "failed"). Callers in the request path ignore it; the
    spool flusher uses it to tell "still owed" from "settled".

    _call_uid / _call_ts: set ONLY by the spool flusher when replaying. Their
    presence means "this call is already on disk, do not spool it again", and
    _call_ts carries the ORIGIN time of the call — not the replay time — so a
    replayed row is recorded in the period it belongs to.

    status: "success" | "error"
    error_message: human-readable provider/bridge error detail (e.g. the
        Bedrock ValidationException text). Persisted TRUNCATED alongside
        error_code — a bare "400" is undiagnosable once the container logs
        have rotated (johann.muehl 2026-07-20: 240 bare-400 rows, root cause
        only recoverable from an app-side job file).
    app_env: already-normalised environment bucket (prod|staging|local) the
        call came from, or None when the app sent no X-App-Env header. The
        caller normalises it (extract_attribution_context); we just persist
        it. Drives the Platform Admin "mode" filter.
    provider: who physically received this call's data. REQUIRED — pass a
        constant from src/activity/providers.py, never a string literal.
        Drives usage_events.provider, which is both the reconciliation key
        against the provider's own billing (e.g. CloudWatch token counts) AND
        the evidence behind the customer-facing EU-residency assurance. It had
        a 'anthropic' default until 2026-08-05; that default silently claimed
        an Anthropic transmission for every caller who did not think about the
        question, including local-only work (docling) and calls to other
        companies (OpenAI Whisper). See providers.py for the full rationale.
        Use ledger_provider_for_backend(backend_config.backend) where a
        backend was resolved — and note that a None backend means the call was
        rejected before routing, i.e. PROVIDER_UNROUTED, not "anthropic".
    provider_meta: extra provider facts merged into provider_metadata
        (e.g. bedrock_model_id, region, aws_request_id for call-level joins
        with AWS invocation logs).
    input_tokens: UNCACHED input (Anthropic/Bedrock usage semantics). Prompt-
        cache traffic goes into cache_read_tokens (0.1x input price) and
        cache_creation_tokens (1.25x input price) — the physical input of a
        call is the sum of all three.
    search_count: server-side web_search invocations this call made (research-
        cloud only) — billed per-search on top of tokens, see src/pricing.py
        WEB_SEARCH_FEE_USD. Defaults to 0 (no-op for every other caller).
    """
    # ── Write-ahead: make the row survivable BEFORE touching the database ──
    # Deliberately the very first thing, and deliberately synchronous: a sync
    # write is not a cancellation point, so nothing after this line — including
    # a cancelled request task or an OOM-kill — can lose the fact that this
    # call happened.
    replaying = _call_uid is not None
    call_uid = _call_uid or ledger_spool.new_call_uid()
    call_ts = _call_ts if _call_ts is not None else time.time()

    # ADR-0011: the row belongs to the ledger of the request's HOME bridge.
    # Live path: capture the middleware-set origin so it travels INTO the
    # spool record. Replay path: the flusher runs outside any request, so the
    # recorded origin is restored into the context — every platform call
    # below (ledger write, billing context, deduction) resolves its target
    # through it. Without this, a spooled foreign row would replay into the
    # LOCAL ledger.
    from src.federation import get_request_origin, set_request_origin
    if replaying:
        set_request_origin(bridge_origin)
    else:
        bridge_origin = bridge_origin or get_request_origin()

    spooled = False
    if not replaying and ledger_spool.spool_enabled():
        spooled = ledger_spool.append_call(call_uid, {
            "app_id": app_id, "user_id": user_id, "agent_id": agent_id,
            "bridge_origin": bridge_origin,
            "workflow_id": workflow_id, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "status": status, "duration_ms": duration_ms,
            "error_code": error_code, "error_message": error_message,
            "app_env": app_env, "provider": provider,
            "provider_meta": provider_meta, "region": region,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "search_count": search_count,
        })

    def _settle(outcome: str) -> str:
        """Single exit. A definitive answer releases the spooled record; any
        other outcome leaves it owed, which is what makes the retry happen."""
        if spooled and ledger_spool.is_definitive(outcome):
            ledger_spool.ack(call_uid, outcome)
        return outcome

    # Validate before anything keys on it: provider drives both the cost
    # branch below and the compliance readout. An unvocabulary value becomes
    # 'unknown' + an ERROR log, never a plausible-looking default.
    provider = normalize_ledger_provider(
        provider, context=f"app={app_id} agent={agent_id} model={model}"
    )

    # Cost from the pricing SSoT. Error calls cost nothing (0.0) — only a
    # successful completion consumes budget.
    call_cost_eur = (
        cost_eur(
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            search_count=search_count,
        )
        if status == "success" else 0.0
    )

    # Resolved from the user row inside the DB block below; initialised here so
    # the post-call deduction (which runs after that block, even if it failed)
    # can always pass it to _deduct_call_cost without a NameError.
    tenant_id: Optional[str] = None

    # App-tier billing policy: a Bridge-owned (app, agent, env) policy row may
    # book this call's COST to an internal account instead of the customer
    # budget. Re-resolved here from the same (app_id, agent_id, app_env) that
    # drove routing (not threaded through the request) so billing always follows
    # the policy definition — a call-site can never forget to pass it. Attribution
    # (actor/tenant/app/agent on the row below) is UNCHANGED; only the deduction
    # target changes and a JSONB marker is added. Cached + fail-open: any error
    # → no billing override (charge the customer as normal), never breaks tracking.
    billing_account: Optional[str] = None
    try:
        from src.routing.app_tier_policy import resolve_app_tier_policy
        _bill_policy = await resolve_app_tier_policy(app_id, agent_id, app_env)
        if _bill_policy is not None:
            billing_account = _bill_policy.billing_account
    except Exception as _bill_e:  # noqa: BLE001 — billing tag must never break tracking
        logger.debug("app_tier_policy billing lookup failed (fail-open): %s", _bill_e)

    # Diagnostic: surface apps that fail to send X-App-Env so the Platform
    # Admin "mode" filter (which depends on app_env) stops being blind.
    # Logged per call (not rate-limited): the noise IS the signal — every
    # unattributed call is a tracking gap that needs an app-side fix.
    # When all callers send the header, the warning disappears entirely.
    if app_env is None and app_id:
        logger.warning(
            "ai_call_writer: missing X-App-Env header app=%s user=%s model=%s "
            "→ usage_events.app_env will be NULL (app-side bug, not bridge). "
            "Fix: include X-App-Env in the app's outbound bridge headers.",
            app_id, user_id, model,
        )

    # Explicit anonymous marker ('anonymous:<grund>' or the legacy report
    # alias '_anonymous') → book to the dedicated anonymous identity. Resolved
    # BEFORE the UUID check below, which would otherwise skip these as
    # "non-UUID string" (the pre-032 behaviour = tracking gap).
    # Default "still owed": every path that does not reach the INSERT and does
    # not name a definitive skip leaves the spooled record for a retry. The
    # bias on a billing path is toward writing the row twice-and-deduplicated,
    # never toward dropping it.
    outcome = OUTCOME_FAILED
    ledger_written = False

    from src.attribution import anonymous_reason
    anon_reason = anonymous_reason(user_id)
    if anon_reason is not None:
        try:
            if await ledger_client.anonymous_identity_present():
                user_id = ANONYMOUS_USER_ID
                agent_id = agent_id or "anonymous"
            else:
                # NOT a skip: the row is owed and stays owed. Running
                # migration 032 makes the spooled record writable, so leaving
                # it unsettled is what turns "we lost those calls" into "they
                # arrive once the migration lands".
                logger.error(
                    "persist_ai_call_activity: anonymous call (reason=%s app=%s) but "
                    "anonymous identity %s is missing — run migration 032. The "
                    "billing row is held in the spool until it exists.",
                    anon_reason, app_id, ANONYMOUS_USER_ID,
                )
                return _settle(OUTCOME_FAILED)
        except Exception as e:  # noqa: BLE001 — tracking must never break the call
            # This exit used to be the quietest hole on the ledger path: a DB
            # blip during the identity probe dropped an anonymous call on the
            # WARNING channel, never reaching the ERROR that is supposed to be
            # the alarm. It is now a transient outcome — the row stays owed.
            logger.error(
                "persist_ai_call_activity: anonymous identity check failed (%s) "
                "— billing row held in the spool for retry", e,
            )
            return _settle(OUTCOME_FAILED)

    try:
        if not user_id:
            # No user → no tenant → cannot write a tenant-scoped row.
            # In-memory prompt-metrics still has it; nothing else to do.
            #
            # Logged, not silent: "no usage_events row" must always have a
            # findable reason. A skip that leaves no trace is indistinguishable
            # from a broken writer, and the ledger is then useless as a
            # measuring instrument — verified the hard way 2026-07-30, when a
            # missing row was read as "the research-cloud path does not work"
            # and cost hours of chasing a defect that did not exist.
            logger.warning(
                "persist_ai_call_activity: no user identity (app=%s agent=%s model=%s) "
                "— NO usage_events row written for this call",
                app_id, agent_id, model,
            )
            return _settle(f"{OUTCOME_SKIPPED}:no_user")

        import uuid as _uuid

        # Validate user_id is a UUID before anything keys on it. usage_events
        # rejects non-UUID values on its uuid-typed column. Apps (or CUI) may
        # send emails or system strings — resolve emails through the shared
        # identity resolver; skip non-user strings with a loud warning
        # (Defensive Programming: tracking gaps must never be silent).
        try:
            _uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            if "@" in str(user_id):
                # Email identity (Engelmann). Resolved via the SHARED resolver
                # rather than this module's own query: src/identity/user_resolver
                # exists exactly to stop this writer from diverging from the
                # budget gate and the lease path on what an identity means, and
                # it already speaks to platform-api (with its own short cache).
                #
                # UnknownUserIdentity is the "no such user" answer — a
                # definitive skip. Anything else (platform-api unreachable) is
                # NOT an answer and must not be turned into one: it propagates
                # to the handler below, where it leaves the row owed.
                from src.identity.user_resolver import (
                    UnknownUserIdentity,
                    resolve_user_id,
                )

                try:
                    user_id = str(await resolve_user_id(user_id))
                except UnknownUserIdentity:
                    _skip_counts[user_id] += 1
                    logger.warning(
                        "persist_ai_call_activity: email not found in users "
                        "(tracking gap #%d) user=%s app=%s → activity skipped. "
                        "Fix: caller should send user UUID, not email.",
                        _skip_counts[user_id], user_id, app_id,
                    )
                    return _settle(f"{OUTCOME_SKIPPED}:email_unknown")
            else:
                # 'system', 'internal', or other non-user strings — semantically
                # not a real user; nothing to persist in the tenant-scoped table.
                _skip_counts[user_id] += 1
                logger.warning(
                    "persist_ai_call_activity: non-UUID X-User-ID=%r app=%s "
                    "(skip #%d) → activity skipped (not a real user). "
                    "Fix: send user UUID or omit X-User-ID for system calls.",
                    user_id, app_id, _skip_counts[user_id],
                )
                return _settle(f"{OUTCOME_SKIPPED}:not_a_user")

        # users ⋈ tenants, via platform-api. `None` is the definitive "no
        # tenant" answer; an UNANSWERABLE lookup raises out of here into the
        # handler below, which is the difference that matters — see
        # ledger_client.load_billing_context.
        _ctx = await ledger_client.load_billing_context(user_id)
        tenant_id = _ctx["tenantId"] if _ctx else None
        billing_mode_text = _ctx["billingMode"] if _ctx else "subscription"
        if not tenant_id:
            # Skip is correct — a usage_events row is tenant-scoped and there
            # is no tenant to scope it to. But say so: this was the last
            # silent exit on the ledger path, and a silently missing row on a
            # BILLING path is the worst kind of quiet failure — spend that
            # nobody can see and nobody can reconcile.
            #
            # The JOIN is inner, so a missing row means EITHER the user id
            # does not exist in `users` OR its tenant_id points nowhere. The
            # old comment lumped both together; the distinction is what tells
            # an operator whether to look at the user or at the tenant.
            _skip_counts[user_id] += 1
            logger.warning(
                "persist_ai_call_activity: no users+tenants row for user=%s "
                "(app=%s agent=%s model=%s, seen %dx) — user missing or its "
                "tenant_id dangling; NO usage_events row written, this call is "
                "NOT metered",
                user_id, app_id, agent_id, model, _skip_counts[user_id],
            )
            return _settle(f"{OUTCOME_SKIPPED}:no_tenant")

        # app_id is an ENUM column — normalise before the write rather than
        # letting Postgres reject it mid-transaction. An unknown value books
        # as NULL (the honest "no app" case) and travels on as app_id_raw,
        # so the call-site stays queryable without costing the row.
        app_id_col, app_id_rejected = normalize_app_id(app_id)

        feature = agent_id or workflow_id or "call"
        event_type = (
            f"ai-call-error:{feature}" if status != "success"
            else f"ai-call:{feature}"
        )
        payload = {
            "feature": feature,
            "model": model,
            # promptTokens = UNCACHED input only (Anthropic usage semantics).
            # Cache traffic is reported separately so the UI can show the
            # physical input — without them a cached agent call displays
            # "10 input tokens" while the priced input is 100k+.
            "promptTokens": input_tokens,
            "completionTokens": output_tokens,
            "cacheReadTokens": cache_read_tokens or 0,
            "cacheCreationTokens": cache_creation_tokens or 0,
            # totalTokens = every token the call physically processed
            # (uncached input + cache reads/writes + output) — matches what
            # costEur prices. Legacy rows (pre cache fields) carry only
            # input+output here.
            "totalTokens": (
                (input_tokens or 0)
                + (cache_read_tokens or 0)
                + (cache_creation_tokens or 0)
                + (output_tokens or 0)
            ),
            "costEur": call_cost_eur,  # priced via src/pricing.py (SSoT)
            "latencyMs": duration_ms,
            "loggedBy": "bridge",  # distinguishes self-log from legacy app POSTs
            # Same value as usage_events.idempotency_key — the join between
            # the audit trail and the money row for one call.
            "callUid": call_uid,
        }
        if error_code:
            payload["errorCode"] = error_code
        if error_message:
            payload["errorMessage"] = str(error_message)[:500]
        if anon_reason is not None:
            # Which call-site declared itself anonymous — the per-<grund>
            # breakdown inside the anonymous bucket.
            payload["anonymousReason"] = anon_reason
        if app_id_rejected:
            # The inbound label was not an app (e.g. a client-id segment
            # like "bridge-jobs"). Keeping it here is the whole point: an
            # unattributed call must stay findable, not merely absent.
            payload["appIdRaw"] = app_id_rejected

        # actor_user_id is a uuid column — only set it when user_id parses.
        actor_uuid = None
        try:
            import uuid as _uuid
            actor_uuid = str(_uuid.UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            actor_uuid = None

        # Usage ledger — structured row with dedicated token/cost columns so
        # the Platform Admin can query across workflow + sandbox without JSONB
        # extraction.  billing_mode/real_cost mapping lives in
        # resolve_ledger_cost (pure, unit-tested) — Bedrock always
        # carries real AWS cost, subscription-served Anthropic is 0. It stays
        # HERE, on the worker: platform-api records the decision, it does not
        # make it.
        bm_enum, real_cost = resolve_ledger_cost(
            billing_mode_text, provider, call_cost_eur
        )

        # Idempotent by construction (ADR-0009 Schritt 1, now over HTTP).
        # Two things make a replay safe rather than a second charge:
        #
        #  • idempotency_key — generated where the CALL happened, not where
        #    the row is written, so a retry an hour later is recognisably
        #    the same call. The column (UNIQUE, migration 016) already
        #    existed and was already used exactly this way by the sandbox
        #    path; the chat/research path simply never set it.
        #  • recorded_at from the call's ORIGIN timestamp, not NOW(). A row
        #    replayed after an outage must be recorded in the period it
        #    belongs to — otherwise a call from the last minute of a month
        #    lands in the next one and the invoice quietly moves.
        #
        # The answer ("written" vs "duplicate") is what the rest of this
        # function needs: did *this* attempt create the row? Moving the write
        # onto HTTP changes nothing about that contract — an unheard answer is
        # simply not an answer, and leaves the call owed in the spool.
        #
        # The audit row is written by the same endpoint, after the money row
        # and only if it was created there. It deliberately does NOT get its
        # own round trip: `activities` has no unique key, so replaying it
        # independently would duplicate the audit trail, and the two rows must
        # not share a fate (2026-08-01: a rejected audit INSERT silently took
        # the billing row down with it).
        _outcome = await ledger_client.write_ai_call({
            "idempotency_key": call_uid,
            "recorded_at": datetime.fromtimestamp(
                call_ts, tz=timezone.utc
            ).isoformat(),
            "actor_user_id": actor_uuid,
            "tenant_id": tenant_id,
            # Same normalised value as the audit row. usage_events.app is
            # plain text and would swallow anything, but a dimension that
            # means one thing in one table and another thing next door is
            # not a dimension — the raw label rides along in
            # provider_metadata.app_id_raw instead.
            "app": app_id_col,
            "app_env": app_env,
            "model": model,
            "provider": provider,
            "region": region,
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cache_read_tokens": cache_read_tokens or 0,
            "cache_creation_tokens": cache_creation_tokens or 0,
            "billing_mode": bm_enum,
            "real_cost_eur": real_cost,
            # hypothetical = priced at pay-per-token rates
            "hypothetical_cost_eur": call_cost_eur,
            "pricing_version": PRICING_VERSION,
            "provider_metadata": {
                "feature": feature,
                "agent_id": agent_id,
                "workflow_id": workflow_id,
                "status": status,
                **({"error_code": error_code} if error_code else {}),
                **({"error_message": str(error_message)[:500]} if error_message else {}),
                # App-tier policy booked this call's cost to an internal
                # account (customer budget NOT charged). Queryable via
                # provider_metadata->>'billing_account'.
                **({"billing_account": billing_account} if billing_account else {}),
                **({"anonymous_reason": anon_reason} if anon_reason is not None else {}),
                # Inbound app label that was not a real app — kept so the
                # call-site of an unattributed call is queryable:
                #   provider_metadata->>'app_id_raw'
                **({"app_id_raw": app_id_rejected} if app_id_rejected else {}),
                **(provider_meta or {}),
            },
            "audit_event_type": event_type,
            "audit_payload": payload,
        })
        ledger_written = _outcome == OUTCOME_WRITTEN
        outcome = OUTCOME_WRITTEN if ledger_written else OUTCOME_DUPLICATE
        if not ledger_written:
            # A replay caught up with a row that had already landed — the
            # spooled record simply outlived its ack. Nothing to repair;
            # logged so a drained backlog is legible afterwards.
            logger.info(
                "persist_ai_call_activity: usage_events row for call %s was "
                "already present — replay settled, no second row, no second "
                "deduction", call_uid,
            )
    except Exception as e:  # noqa: BLE001 — tracking must never break the call
        # ERROR, not WARNING: reaching here means NO usage_events row for a call
        # that really happened — spend nobody can see and nobody can reconcile.
        # The call itself is deliberately left intact (the writer must never
        # break a user-facing request), but the gap has to be alarm-worthy.
        logger.error(
            "persist_ai_call_activity: LEDGER WRITE FAILED (app=%s agent=%s "
            "model=%s provider=%s) — this call is NOT metered yet%s: %s",
            app_id, agent_id, model, provider,
            " (held in the write-ahead spool, will be retried)" if spooled else
            " AND NOT SPOOLED — this row is lost",
            e,
        )

    # Post-call budget deduction — bound to the ledger row (ADR-0009 Schritt 1).
    #
    # The ledger row is the authoritative usage record; the deduction is a
    # running tally derived from it, so the pre-call gate has something to gate
    # against. `ledger_written` is the whole condition: deduct exactly when THIS
    # attempt created the row.
    #
    # This replaces an ordering that was only ever asserted in comments. The
    # deduction used to sit outside the try above and ran even when that try had
    # fallen into its except — so a partial failure (a rejected INSERT on an
    # otherwise healthy pool, as on 2026-08-01) deducted budget with no row to
    # show for it: a charge nobody can reconstruct. And once a replay exists,
    # an unbound deduction would charge a second time for the same call, because
    # apply_budget_deduction has no dedup key.
    #
    # The deduction is therefore DEFERRED, not lost, when the write fails: the
    # spool holds the call, and the deduction happens when the row lands.
    #
    # Anonymous calls have no budget semantics (internal bucket, real_cost 0).
    # billing_account set → an app-tier policy books this call to an internal
    # account; the customer's (project) budget must NOT be deducted. The usage
    # row already carries the billing_account marker + full attribution.
    if not ledger_written:
        if outcome == OUTCOME_FAILED and call_cost_eur > 0:
            logger.warning(
                "post-call deduction DEFERRED (app=%s user=%s %.6f EUR): the "
                "ledger row is not written yet, so there is nothing to derive a "
                "tally from. The call is held in the write-ahead spool; the "
                "deduction happens when the row lands.",
                app_id, user_id, call_cost_eur,
            )
    elif billing_account:
        logger.info(
            "app_tier_policy: cost booked to internal account %r (app=%s agent=%s) "
            "— customer deduction skipped (%.6f EUR)",
            billing_account, app_id, agent_id, call_cost_eur,
        )
    elif user_id and app_id and call_cost_eur > 0 and user_id != ANONYMOUS_USER_ID:
        await _deduct_call_cost(
            user_id, app_id, call_cost_eur, workflow_id, tenant_id, call_ts,
        )

    return _settle(outcome)
