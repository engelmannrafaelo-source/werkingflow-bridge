"""job_id format — a job carries the id of the bridge whose store holds it.

ADR-0012. The two bridges run two SEPARATE job stores (ADR-0009: no shared
database, deliberately). Under the two-tier overflow pool (ADR-0010) a
`POST /v1/jobs` can be served by the PEER bridge — nginx's
`proxy_next_upstream http_429` walks the local workers and then the backup
upstream, which IS the other bridge. The row is then written over there, while
the subsequent `GET /v1/jobs/{id}` is routed independently and lands on
whichever bridge the poll's own pool selection picks. Every miss surfaced as
`404 Async job not found (unknown id, or expired)` — a healthy, running job
reported as gone (measured deterministically 2026-09-03: 20/20 polls 404).

The fix is to make the job SAY where it lives, so the poll can follow it
without anybody keeping routing state:

    job_<home>_<32 hex>        e.g. job_prod_1f0c…   (tagged, since ADR-0012)
    job_<32 hex>               e.g. job_1f0c…        (legacy, pre-ADR-0012)

`<home>` is this bridge's own id — `BRIDGE_ORIGIN_ID`, the same value nginx
stamps as `X-Bridge-Origin` from `${BRIDGE_ID}` (ADR-0011). It is the id of
the bridge that STORES the job, which is the executing one: the job row and
its executor's self-call belong to the bridge that accepted the POST
(ADR-0011 point 4 lists `jobs/store_client` as `domain="local"`). That is
NOT necessarily the job's identity/budget home, which travels separately in
`attribution.bridge_origin` — the two are different questions and are
deliberately answered by different fields.

nginx routes on the same grammar (`$job_home_marker` in docker/nginx.conf).
`test_job_id_matches_nginx_regex` pins the two against each other so the
router and the app can never disagree about what a job id is.

LEGACY / TRANSITION RULE (deliberately temporary, not a silent fallback)
-----------------------------------------------------------------------
Ids minted before this ADR carry no marker. They cannot be routed — there is
nothing in them to route on — so they keep exactly the pre-ADR behaviour:
answered by whichever bridge the poll reaches, with a WARNING per lookup.
This exists for one bounded window: the rollout, in which jobs submitted
before the deploy are still being polled after it. It expires by itself,
because the store's TTL cleanup (`cleanup_old`, dev 45 min / prod ~75 min)
removes every pre-deploy row within about an hour — after that a legacy id
can only be a stale client, and the warning says so.

Anything that is NEITHER form is a broken id, not a missing one: it raises
JobIdMalformed and the route answers 400, never a 404 that reads like
"expired".
"""
from __future__ import annotations

import os
import re
import uuid

# The marker charset is deliberately narrow: lowercase alphanumeric plus
# hyphen, no underscore. The underscore is the field separator, so allowing it
# in the marker would make `job_a_b_<hex>` ambiguous — and nginx's map regex
# (which cannot backtrack across an ambiguous split) would silently pick a
# different field than Python does. Same charset as nginx's $bridge_origin_out
# ([a-z0-9-]).
_HOME_RE = r"[a-z0-9][a-z0-9-]{0,15}"
_UID_RE = r"[0-9a-f]{32}"

# The ONE grammar. docker/nginx.conf mirrors the tagged form; the test suite
# holds the two together.
TAGGED_JOB_ID_RE = re.compile(rf"^job_(?P<home>{_HOME_RE})_(?P<uid>{_UID_RE})$")
LEGACY_JOB_ID_RE = re.compile(rf"^job_(?P<uid>{_UID_RE})$")

# The regex nginx uses to pull the marker out of a poll URI. Kept HERE, next to
# the Python grammar it must agree with, and asserted equal by the test suite —
# a copy that lives only in nginx.conf drifts the first time somebody widens
# one side. docker/nginx.conf carries this string verbatim.
NGINX_JOB_MARKER_REGEX = rf"^/v1/jobs/job_(?<jhm>{_HOME_RE})_{_UID_RE}$"


class JobIdMalformed(ValueError):
    """The id is neither a tagged nor a legacy job id. A caller sending this
    has a bug (truncated id, wrong variable, hand-typed probe) — it is NOT the
    same situation as an id we simply do not have, and must not be answered
    with the 404 that means "unknown or expired"."""


class JobHomeUnconfigured(RuntimeError):
    """This process cannot name its own bridge (BRIDGE_ORIGIN_ID unset or not
    a valid marker), so it can neither mint a routable job id nor decide
    whether an incoming id belongs to it.

    Fail CLOSED. Minting an untagged id instead would reproduce exactly the
    unroutable-job bug this module exists to remove, and it would do it
    silently — the deploy would look healthy while every cross-bridge poll
    404s again. Same fail polarity as ADR-0011 point 5: an unconfigured
    federation identity is a deploy error, not a transient one.
    """


def home_bridge_id() -> str:
    """This bridge's marker — the id of the store that holds jobs created here.

    Reuses BRIDGE_ORIGIN_ID (ADR-0011) rather than introducing a second env
    var for the same fact: a bridge that answers "who am I" differently for
    billing and for job storage is a bridge with two identities, and the drift
    between them would be invisible until a poll went missing.
    """
    raw = os.getenv("BRIDGE_ORIGIN_ID", "").strip().lower()
    if not raw:
        raise JobHomeUnconfigured(
            "BRIDGE_ORIGIN_ID is unset — this bridge cannot name itself, so a "
            "job created here could not be found again from the peer bridge "
            "(ADR-0012). Set it on the worker containers (secrets/platform.env) "
            "to the same value nginx uses as ${BRIDGE_ID} on this host."
        )
    if not re.fullmatch(_HOME_RE, raw):
        raise JobHomeUnconfigured(
            f"BRIDGE_ORIGIN_ID={raw!r} is not a usable job-home marker — it must "
            f"match {_HOME_RE} (lowercase alphanumeric/hyphen, no underscore: the "
            f"underscore separates the marker from the id in job_<home>_<hex>)."
        )
    return raw


def new_job_id() -> str:
    """Mint a job id that names this bridge. Raises JobHomeUnconfigured."""
    return f"job_{home_bridge_id()}_{uuid.uuid4().hex}"


def parse_home(job_id: str) -> str | None:
    """The home marker of `job_id`, or None for a legacy (untagged) id.

    Raises JobIdMalformed when the id is neither form.
    """
    if not isinstance(job_id, str):
        raise JobIdMalformed(f"job id must be a string, got {type(job_id).__name__}")
    tagged = TAGGED_JOB_ID_RE.match(job_id)
    if tagged:
        return tagged.group("home")
    if LEGACY_JOB_ID_RE.match(job_id):
        return None
    raise JobIdMalformed(
        f"malformed job id {job_id!r}: expected job_<home>_<32 hex> "
        f"(or the legacy job_<32 hex> from before ADR-0012)"
    )


def is_legacy(job_id: str) -> bool:
    """True for an untagged, pre-ADR-0012 id. Raises JobIdMalformed on junk."""
    return parse_home(job_id) is None
