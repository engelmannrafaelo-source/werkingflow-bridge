"""
Boot invariant: a worker that declares DB-backed features must have a database.

Both prod workers were recreated without the platform overlay on 2026-08-02.
BRIDGE_GENERIC_JOBS_ENABLED (base compose) survived, BRIDGE_DB_URL (overlay) did
not. The workers reported healthy while /v1/jobs answered 503 and every budget
and activity hook was skipped — and the guards that would normally catch a
half-configured worker (plan catalog, app registry) sit inside the
`is_db_enabled()` branch of the lifespan, so none of them could fire.

These cases pin the invariant in both directions: it must refuse the drifted
state, and it must stay silent for a deployment that legitimately runs without
a database.
"""
import importlib.util
import pathlib

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No variable leaks in from the developer's shell or a prior case.
    BRIDGE_SERVICE_TOKEN matters since ADR-0009 Weg b: it configures the
    platform-api store stage and legitimately satisfies the invariant."""
    monkeypatch.delenv("BRIDGE_GENERIC_JOBS_ENABLED", raising=False)
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    monkeypatch.delenv("BRIDGE_SERVICE_TOKEN", raising=False)


def _load_pristine(rel_path, name):
    """Load a module straight from its file, bypassing sys.modules entirely.

    Neither reads nor writes the import cache, so nothing else in the session
    observes this — the reason an earlier sys.modules-swapping version of this
    fixture broke 14 unrelated tests in tests/jobs/ that hold references to the
    modules it re-imported.
    """
    path = pathlib.Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def check(monkeypatch):
    """The invariant, bound to the REAL database predicate.

    Sibling suites install MagicMock stubs for heavy modules in sys.modules at
    import time and never remove them (tests/research_cloud/*,
    tests/unit/test_ai_call_writer.py, …). A MagicMock's is_db_enabled() is
    truthy, so whether these cases see a real predicate or a leftover stub would
    otherwise depend on collection order — and with a stub in place the
    invariant reports "database present" for every case, passing the tests that
    should fail and failing the ones that should pass.
    """
    import src.config_invariants as invariants

    real_client = _load_pristine("src/db/client.py", "_pristine_db_client")
    monkeypatch.setattr(invariants, "is_db_enabled", real_client.is_db_enabled)
    return invariants.assert_declared_db_features_have_a_database


DSN = "postgresql://user:pw@host:5432/bridge"


# --- the state that must be refused ----------------------------------------

def test_declared_jobs_without_db_url_refuses_to_boot(check, monkeypatch):
    """The exact 2026-08-02 drift: flag from base, URL lost with the overlay."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    with pytest.raises(RuntimeError) as exc:
        check(db_client_available=True)
    msg = str(exc.value)
    assert "BRIDGE_DB_URL" in msg
    # The message must carry the fix, not just the symptom — it surfaces in a
    # container log, where nobody has the compose files in front of them.
    assert "platform" in msg.lower()
    assert "--no-deps" in msg


def test_declared_jobs_without_db_client_refuses_to_boot(check, monkeypatch):
    """Same hollowness, different cause: asyncpg missing from the image."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_DB_URL", DSN)
    with pytest.raises(RuntimeError) as exc:
        check(db_client_available=False)
    assert "asyncpg" in str(exc.value)


def test_missing_client_is_reported_even_without_a_url(check, monkeypatch):
    """Both defects at once must still name the import failure rather than
    stopping at the more obvious missing URL."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    with pytest.raises(RuntimeError) as exc:
        check(db_client_available=False)
    assert "asyncpg" in str(exc.value)


@pytest.mark.parametrize("truthy", ["true", "TRUE", "  True  ", "1", "yes"])
def test_every_truthy_spelling_is_covered(check, monkeypatch, truthy):
    """Guard and endpoints share one parser, so no spelling can enable the
    routes while slipping past the check."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", truthy)
    with pytest.raises(RuntimeError):
        check(db_client_available=True)


# --- states that must boot normally -----------------------------------------

def test_declared_jobs_with_db_url_boots(check, monkeypatch):
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_DB_URL", DSN)
    check(db_client_available=True)  # must not raise


def test_undeclared_without_db_boots(check):
    """A deployment that wants no database leaves the flag at its default.
    The invariant must not turn that into an outage."""
    check(db_client_available=True)  # must not raise


def test_undeclared_without_db_or_client_boots(check):
    """No feature declared, no database, no driver — nothing is promised, so
    nothing is broken. The check must not reach for the driver either."""
    check(db_client_available=False)  # must not raise


def test_undeclared_with_db_boots(check, monkeypatch):
    """A database without the jobs feature is valid too — the budget gate uses
    the same pool."""
    monkeypatch.setenv("BRIDGE_DB_URL", DSN)
    check(db_client_available=True)  # must not raise


@pytest.mark.parametrize("falsy", ["false", "0", "no", "", "off"])
def test_falsy_spellings_do_not_demand_a_database(check, monkeypatch, falsy):
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", falsy)
    check(db_client_available=True)  # must not raise


# --- ADR-0009 Weg b: platform-api-only workers (the worker-host) -------------
# The 2026-08-31 cutover failed exactly here: the invariant predates Weg b and
# refused a correctly configured DB-free worker (BRIDGE_SERVICE_TOKEN set, no
# BRIDGE_DB_URL) — the designed end state of the worker-host.

def test_platform_stage_only_boots(check, monkeypatch):
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "svc-token")
    check(db_client_available=True)  # must not raise


def test_platform_stage_only_needs_no_db_driver(check, monkeypatch):
    """platform-api-only never touches asyncpg — a missing driver must not
    block the boot when no direct-DB stage is declared."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "svc-token")
    check(db_client_available=False)  # must not raise


def test_declared_db_with_broken_driver_refuses_even_with_platform_stage(
    check, monkeypatch
):
    """A DECLARED direct-DB stage whose driver cannot import is hollow — the
    platform stage does not excuse it (the declared fallback would be dead)."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_DB_URL", DSN)
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "svc-token")
    with pytest.raises(RuntimeError) as exc:
        check(db_client_available=False)
    assert "asyncpg" in str(exc.value)


def test_refusal_message_names_both_stages(check, monkeypatch):
    """The log line is the only thing an operator sees — it must name BOTH
    ways out (overlay/DB and service token), not just the pre-Weg-b one."""
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    with pytest.raises(RuntimeError) as exc:
        check(db_client_available=True)
    msg = str(exc.value)
    assert "BRIDGE_DB_URL" in msg and "BRIDGE_SERVICE_TOKEN" in msg


def test_platform_stage_signal_matches_store_client(monkeypatch):
    """Lockstep pin: the invariant's platform-stage signal and
    store_client.is_store_available() must key off the same variable. If
    store_client ever changes its signal, this must fail loudly."""
    import src.config_invariants as invariants

    monkeypatch.delenv("BRIDGE_SERVICE_TOKEN", raising=False)
    assert invariants._platform_stage_configured() is False
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "svc-token")
    assert invariants._platform_stage_configured() is True

    store_client_src = (
        pathlib.Path(__file__).resolve().parents[2] / "src/jobs/store_client.py"
    ).read_text()
    assert 'os.getenv("BRIDGE_SERVICE_TOKEN")' in store_client_src, (
        "store_client.is_store_available() no longer keys the platform stage "
        "off BRIDGE_SERVICE_TOKEN — update _platform_stage_configured() in "
        "src/config_invariants.py to match, then this pin."
    )
