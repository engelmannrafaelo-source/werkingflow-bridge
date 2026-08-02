"""
Boot-time configuration invariants.

Deliberately dependency-light: these run before the app is up and must be
verifiable without importing it. `src.main` pulls in the Claude Code SDK and the
whole worker stack, which is neither available nor relevant when the question is
merely "is this container configured coherently?".

Companion to the fail-fast checks already in the lifespan (billing integrity,
plan catalog, app registry). Same doctrine: a worker that cannot do its job
correctly must refuse to start rather than serve traffic that looks healthy.
"""
from src.db.client import is_db_enabled
# One parser for the flag, shared with the endpoints it gates. Two readings
# would be two truths, and they must never disagree about what is declared.
from src.jobs.routes import _generic_jobs_enabled


def assert_declared_db_features_have_a_database(*, db_client_available: bool) -> None:
    """Refuse to boot a worker that declares DB-backed features without a database.

    BRIDGE_GENERIC_JOBS_ENABLED is declared in the BASE compose file;
    BRIDGE_DB_URL arrives through the platform overlay
    (docker-compose-*-platform.yml → secrets/platform.env). That split is
    deliberate — and it is precisely why the two can drift apart: recreate a
    worker with only the base file and the flag survives while the URL vanishes.

    Both prod workers ran in exactly that state until 2026-08-02, reporting
    healthy the entire time. /v1/jobs answered 503, and the budget gate, activity
    tracking and post-call deduction were skipped on every request — visible only
    as a single INFO line at startup.

    The sting: the fail-fast checks that would catch a half-configured worker
    (plan catalog, app registry) live INSIDE the `is_db_enabled()` branch of the
    lifespan. Lose the URL and none of them run. This is the one state in which
    every existing guard is unreachable, which is why it needs its own.

    Declaring a feature while withholding its database is a configuration error,
    not a runtime mode. A deployment that genuinely wants no database leaves the
    flag at its default ("false", see src/jobs/routes.py) and is unaffected.

    Args:
        db_client_available: whether `src.db.client` imported successfully in the
            caller. Passed in rather than re-derived, so the invariant judges the
            same import result the worker will actually use.

    Raises:
        RuntimeError: the deployment declares DB-backed features but cannot reach
            a database. The message carries the fix, not just the symptom — it
            surfaces in a container log, where nobody has the compose files to hand.
    """
    if not _generic_jobs_enabled():
        return

    if not db_client_available:
        raise RuntimeError(
            "BRIDGE_GENERIC_JOBS_ENABLED=true but the Bridge DB client failed to "
            "import (asyncpg missing from the image?). /v1/jobs, the budget gate "
            "and activity tracking all require Postgres. Fix the image, or unset "
            "BRIDGE_GENERIC_JOBS_ENABLED if this worker is meant to run without a DB."
        )

    if not is_db_enabled():
        raise RuntimeError(
            "BRIDGE_GENERIC_JOBS_ENABLED=true but BRIDGE_DB_URL is not set — the "
            "platform overlay is missing from this container. Recreate it with BOTH "
            "compose files, e.g.:\n"
            "    docker compose -f docker/docker-compose-prod.yml \\\n"
            "                   -f docker/docker-compose-prod-platform.yml \\\n"
            "                   up -d --no-deps <worker>\n"
            "Refusing to start: this worker would report healthy while /v1/jobs "
            "returns 503 and every budget and activity hook is silently skipped."
        )
