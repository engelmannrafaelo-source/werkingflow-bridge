"""Gate 2: server-owned admission and composition of the CLI run.

Nothing here is selectable by a request: the policy, the place, the credential
and the counter come from the server's own configuration file, named by
``AGENTISCHER_LAUF_KONFIG``. Without that file the capability stays
unavailable (422 ``faehigkeit_nicht_verfuegbar``), loudly logged at start.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from src.version import METADATA_ERRORS, read_version

from .adapter import Adapters, RunError

LOG = logging.getLogger(__name__)
KONFIG_VARIABLE = "AGENTISCHER_LAUF_KONFIG"


@dataclass(frozen=True)
class Politik:
    modelle: frozenset[str]
    werkzeuge: frozenset[str]
    max_zeit_s: int
    max_tokens_gesamt: int
    max_turns: int
    max_tokens_je_antwort: int
    # The shared key cannot prove which app calls; off unless decided.
    geteilter_schluessel: bool = False


def mandant(request, politik: Politik) -> str:
    """Tenant scope from the authenticated principal, never from the body."""
    from src.auth import _principal_app_from_client_id

    principal = getattr(request.state, "principal", None)
    if principal is None or (principal.is_legacy and not politik.geteilter_schluessel):
        raise HTTPException(403, "verified tenant scope required")
    app_id = request.headers.get("x-app-id") or _principal_app_from_client_id(
        request.headers.get("x-client-id")
    )
    if not app_id or not principal.may_use_app(app_id):
        raise HTTPException(403, "verified tenant scope required")
    return f"{principal.name}/{app_id}"


def pruefe(politik: Politik, body) -> None:
    """Requested tools must be permitted, every limit explicitly set and served."""
    run = body.agentischer_lauf
    if "grenzen" not in run.model_fields_set or not {"max_turns", "max_tokens"} <= set(
        body.model_fields_set
    ):
        raise RunError("auftrag_ungueltig")
    phasen = [(body.model, run.werkzeuge, body.max_turns, body.max_tokens)]
    if run.pruefung:
        review = run.pruefung
        phasen.append((review.model, review.werkzeuge, review.max_turns, review.max_tokens))
    for model, werkzeuge, turns, max_tokens in phasen:
        if model not in politik.modelle:
            raise RunError("faehigkeit_nicht_verfuegbar")
        if any(t.name not in politik.werkzeuge for t in werkzeuge):
            raise RunError("faehigkeit_nicht_verfuegbar")
        if turns > politik.max_turns or max_tokens > politik.max_tokens_je_antwort:
            raise RunError("faehigkeit_nicht_verfuegbar")
    if (
        run.grenzen.zeit_s > politik.max_zeit_s
        or run.grenzen.tokens_gesamt > politik.max_tokens_gesamt
    ):
        raise RunError("faehigkeit_nicht_verfuegbar")


def zulassung(politik: Politik):
    async def admit(request, body):
        tenant = mandant(request, politik)
        if body is not None:
            pruefe(politik, body)
        return tenant

    return admit


class MessagesZaehler:
    """Provider-counted start input (Messages count_tokens), no estimate."""

    def __init__(self, basis_url: str, zugang, *, oauth: bool):
        self.url = basis_url.rstrip("/") + "/v1/messages/count_tokens"
        self.zugang = zugang
        self.oauth = oauth

    async def __call__(self, model, systemtext, prompt):
        import httpx

        token = self.zugang()
        headers = {"anthropic-version": "2023-06-01"}
        if self.oauth:
            headers |= {"authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
        else:
            headers["x-api-key"] = token
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if systemtext:
            body["system"] = systemtext
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError:
            raise RunError("anbieter_fehler") from None
        if response.status_code in (429, 503, 529):
            raise RunError("kapazitaet_belegt", retryable=True, retry_after_s=60)
        if response.status_code != 200:
            raise RunError("anbieter_fehler")
        try:
            return response.json()["input_tokens"]
        except (ValueError, KeyError, TypeError):
            raise RunError("anbieter_fehler") from None


def pruefe_platz(wurzel: Path, settings: Path, *, uid=None, stat=os.stat, access=os.access) -> None:
    """The CLI run starts only inside an isolated place (ACLIR P2). Before,
    "isolation present" was concluded from the settings file existing, and
    dedicated_uid:false was accepted: in the bridge's main container with a
    stray settings file that left process group and hook, no own UID, no
    egress proxy. Required now, each checked at start, loud on failure:

    * not root — the place sweep (stop_uid_processes) kills every process of
      this UID, which must be the place's alone;
    * platz_wurzel belongs to this UID and is closed to group and others
      (the place container: /arbeit/<platz>, mode 0700);
    * the hook settings belong to root and this UID cannot write them, so the
      model cannot loosen its own hook.
    """
    uid = os.getuid() if uid is None else uid
    gruende = []
    if uid == 0:
        gruende.append("laeuft als root")
    try:
        info = stat(wurzel, follow_symlinks=False)
    except OSError:
        gruende.append(f"platz_wurzel {wurzel} fehlt")
    else:
        if info.st_uid != uid or info.st_mode & 0o077:
            gruende.append(f"platz_wurzel {wurzel} gehoert nicht allein dieser UID")
    try:
        info = stat(settings)
    except OSError:
        gruende.append(f"settings {settings} fehlt")
    else:
        if info.st_uid != 0 or access(settings, os.W_OK):
            gruende.append(f"settings {settings} nicht root-eigen oder fuer den Platz beschreibbar")
    if gruende:
        LOG.error("agentischer Lauf: kein isolierter Platz, Start abgebrochen: %s", "; ".join(gruende))
        raise RunError("faehigkeit_nicht_verfuegbar")


def laufrevision(konfig: dict) -> str:
    """The revision every run states in herkunft.bridge_revision: the git_sha
    of this image's build metadata, the same value GET /v1/version answers.
    A caller checks both against the revision it released; a second, hand-kept
    value let the version probe pass and then failed every paid run
    (BRUMBAU-N1 D1). ``revision`` in the configuration is optional and only a
    cross-check: a different value is a misconfiguration and stops the start."""
    try:
        sha = read_version().git_sha
    except METADATA_ERRORS as exc:
        # A hand build without GIT_COMMIT carries git_sha "unknown"
        # (compose default ${GIT_COMMIT:-unknown}): the whole worker then does
        # not start while AGENTISCHER_LAUF_KONFIG is set. Say how to fix it.
        LOG.error(
            "agentischer Lauf: keine gueltige Build-Revision in "
            "src/build-metadata.json (%s: %s), Start abgebrochen. Das Image mit "
            "GIT_COMMIT=$(git rev-parse HEAD) bauen (scripts/bridge-deploy.sh "
            "setzt es) oder AGENTISCHER_LAUF_KONFIG fuer diesen Worker weglassen; "
            "solange beides fehlt, startet der Worker nicht.",
            type(exc).__name__, exc,
        )
        raise RunError("faehigkeit_nicht_verfuegbar") from None
    angegeben = konfig.get("revision")
    if angegeben is not None and str(angegeben) != sha:
        LOG.error(
            "agentischer Lauf: Konfig-revision %r weicht von der Build-Revision %s ab "
            "(/v1/version), Start abgebrochen",
            angegeben, sha,
        )
        raise RunError("faehigkeit_nicht_verfuegbar")
    return sha


def verdrahte(konfig: dict):
    """Compose runtime and policy; every missing setting fails at start."""
    from . import http
    from .cli import CliLauf
    from .cli_platz import CliPlatz
    from .runtime import Runtime
    from .storage import Storage

    if konfig.get("dedicated_uid") is not True:
        LOG.error("agentischer Lauf: dedicated_uid muss true sein (ACLIR P2), Start abgebrochen")
        raise RunError("faehigkeit_nicht_verfuegbar")
    pruefe_platz(Path(konfig["platz_wurzel"]), Path(konfig["settings"]))
    revision = laufrevision(konfig)
    variable = konfig["zugang_variable"]

    def zugang():
        value = os.environ.get(variable)
        if not value:
            raise RunError("faehigkeit_nicht_verfuegbar")
        return value

    platz = CliPlatz(
        Path(konfig["platz_wurzel"]),
        befehl=list(konfig["befehl"]),
        settings=Path(konfig["settings"]),
        zugang_variable=konfig.get("cli_zugang_variable", "CLAUDE_CODE_OAUTH_TOKEN"),
        dedicated_uid=True,
        max_bytes=int(konfig["max_bytes"]),
        **({"max_strom_bytes": int(konfig["max_strom_bytes"])}
           if "max_strom_bytes" in konfig else {}),
    )
    platz.bereit()
    zaehler = MessagesZaehler(konfig["zaehler_url"], zugang, oauth=bool(konfig["oauth"]))
    adapters = Adapters()
    for name, cli_modell in konfig["modelle"].items():
        adapters.register_cli(
            name, CliLauf(platz, cli_modell=cli_modell, zugang=zugang, zaehler=zaehler)
        )
    politik = Politik(
        modelle=frozenset(konfig["modelle"]),
        werkzeuge=frozenset(konfig["werkzeuge"]),
        max_zeit_s=int(konfig["max_zeit_s"]),
        max_tokens_gesamt=int(konfig["max_tokens_gesamt"]),
        max_turns=int(konfig["max_turns"]),
        max_tokens_je_antwort=int(konfig["max_tokens_je_antwort"]),
        geteilter_schluessel=bool(konfig.get("geteilter_schluessel", False)),
    )
    runtime = Runtime(
        adapters,
        Storage(Path(konfig["speicher"])),
        revision=revision,
        max_bytes=int(konfig["max_bytes"]),
    )
    http.configure(runtime, zulassung(politik))
    return runtime, politik


def verdrahte_aus_konfiguration():
    path = os.environ.get(KONFIG_VARIABLE)
    if not path:
        LOG.warning(
            "agentischer Lauf nicht verdrahtet (%s fehlt): 422 faehigkeit_nicht_verfuegbar",
            KONFIG_VARIABLE,
        )
        return None
    return verdrahte(json.loads(Path(path).read_text()))
