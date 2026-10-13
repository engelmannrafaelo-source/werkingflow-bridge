"""Agentic run over the CLI place: the CLI drives its own tool loop.

Not a hard total bound: ``hard_token_bound`` is False and every result says so
in ``grenze``. Declared limits instead, each ending the run loudly with its
cause (evidence from CLI 2.1.285 in docs/agentischer-lauf-cli.md):

- rounds: ``--max-turns``; the CLI ends with ``error_max_turns``.
- output per answer: ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is sent as ``max_tokens``
  of every request; a higher reported output is ``grenze_tokens``.
- time: ``grenzen.zeit_s``; the process group is killed, ``grenze_zeit``.
- input: a server-owned counter measures the start prompt before the start.
- running usage: with ``--include-partial-messages`` every answer streams its
  ``message_start`` (input and cache counters) and ``message_delta`` (final
  output). Once the sum exceeds the remaining budget the run is killed. The
  CLI emits nothing before the request is sent, so one answer can overshoot by
  its own input plus ``max_tokens``; the result reports what was used.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from src.erkunder.kind import lesezugriff

from .adapter import TOKEN_KEYS, RunError
from .cli_platz import CliAuftrag
from .sandbox import canonical, sha

# Contract tool names onto the CLI tools that implement exactly that access.
CLI_WERKZEUGE = {
    "Read": ("Read", "Glob", "Grep"),
    "List": ("Glob",),
    "Write": ("Write", "Edit"),
    # Computation runs where the Erkunder computes today: inside the place
    # (own UID, read-only image, egress only through its proxy). A fresh
    # container per call (BashPlace) would need a Docker daemon the worker can
    # reach, which is host root unless the daemon runs rootless (ABASHR
    # condition 1, BRUMBAU 13.10.2026). Only on a verified place.
    "Bash": ("Bash",),
}
# Werkzeuge that execute code: only where the place's own UID is the boundary.
CLI_NUR_IM_PLATZ = frozenset({"Bash"})
# Answers to an overloaded or rate-limited request; repeatable later.
KAPAZITAET_STATUS = {429, 503, 529}


class Grenze(RunError):
    """A declared limit stopped the run; ``ursache`` names the check."""

    def __init__(self, code, ursache):
        super().__init__(code)
        self.ursache = ursache


def zaehler_wert(value, ursache="nutzung_unbekannt"):
    if type(value) is not int or value < 0:
        raise Grenze("grenze_tokens", ursache)
    return value


class StromZaehler:
    """Running usage of one phase from the CLI's partial-message stream."""

    def __init__(self, budget: int, max_tokens: int):
        self.budget = budget
        self.max_tokens = max_tokens
        self.antworten: dict[str, dict[str, int]] = {}
        self.aktuell: str | None = None
        self.ergebnis: dict | None = None

    def verbraucht(self) -> int:
        return sum(sum(a.values()) for a in self.antworten.values())

    def zeile(self, line: dict) -> None:
        if line.get("type") == "result":
            self.ergebnis = line
            return
        if line.get("type") != "stream_event":
            return
        event = line.get("event")
        if not isinstance(event, dict):
            raise RunError("protokoll_unvollstaendig")
        if event.get("type") == "message_start":
            message = event.get("message")
            ident = message.get("id") if isinstance(message, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(ident, str) or not isinstance(usage, dict):
                raise Grenze("grenze_tokens", "nutzung_unbekannt")
            if ident in self.antworten:
                raise RunError("protokoll_unvollstaendig")
            self.antworten[ident] = {
                "input_tokens": zaehler_wert(usage.get("input_tokens")),
                # Absent cache counters mean no cache use for this request.
                "cache_read_input_tokens": zaehler_wert(usage.get("cache_read_input_tokens", 0)),
                "cache_creation_input_tokens": zaehler_wert(
                    usage.get("cache_creation_input_tokens", 0)
                ),
                "output_tokens": zaehler_wert(usage.get("output_tokens", 0)),
            }
            self.aktuell = ident
        elif event.get("type") == "message_delta":
            usage = event.get("usage")
            if self.aktuell is None or not isinstance(usage, dict):
                raise Grenze("grenze_tokens", "nutzung_unbekannt")
            antwort = self.antworten[self.aktuell]
            for key in TOKEN_KEYS:
                if usage.get(key) is not None:
                    antwort[key] = zaehler_wert(usage[key])
            if "output_tokens" not in usage:
                raise Grenze("grenze_tokens", "nutzung_unbekannt")
            if antwort["output_tokens"] > self.max_tokens:
                raise Grenze("grenze_tokens", "ausgabe_ueber_grenze")
        else:
            return
        if self.verbraucht() > self.budget:
            raise Grenze("grenze_tokens", "laufende_nutzung_ueber_budget")

    def abschluss(self) -> dict[str, int]:
        """Result totals win where the CLI made calls it did not stream."""
        summe = {k: sum(a[k] for a in self.antworten.values()) for k in TOKEN_KEYS}
        usage = (self.ergebnis or {}).get("usage")
        if not isinstance(usage, dict):
            raise Grenze("grenze_tokens", "nutzung_unbekannt")
        gesamt = {
            k: max(
                summe[k],
                zaehler_wert(usage.get(k, 0 if k.startswith("cache_") else None)),
            )
            for k in TOKEN_KEYS
        }
        if sum(gesamt.values()) > self.budget:
            raise Grenze("grenze_tokens", "nutzung_nachtraeglich_ueber_budget")
        return gesamt


def chunks_aus_strom(zeilen: list[dict]) -> list[dict]:
    """Raw stream-json in the chunk shape BRTRUNC's antwort_ende reads."""
    from src.claude_cli import cli_fortsetzung

    chunks, ausstehend = [], None
    for line in zeilen:
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        if line.get("type") == "user":
            turn = {"type": "user", "content": message.get("content")}
            if "isSynthetic" in line:
                turn["isSynthetic"] = line["isSynthetic"]
            ausstehend = cli_fortsetzung(turn) or ausstehend
        elif line.get("type") == "assistant":
            chunk = {
                "type": "assistant",
                "content": message.get("content") or [],
                "stop_reason": message.get("stop_reason"),
            }
            if line.get("error"):
                chunk["cli_error"] = line["error"]
            if ausstehend:
                chunk[ausstehend] = True
                ausstehend = None
            chunks.append(chunk)
        elif line.get("type") == "result":
            chunks.append(
                {
                    k: line.get(k)
                    for k in ("type", "subtype", "is_error", "num_turns", "stop_reason", "api_error")
                }
            )
    return chunks


def werkzeugprotokoll(zeilen, phase, cwd: Path, material):
    """Every tool call the CLI streamed, matched to its result by id."""
    eintraege, offen = [], {}
    for line in zeilen:
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if line.get("type") == "assistant" and block.get("type") == "tool_use":
                call_id, name, args = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not call_id or call_id in offen:
                    raise RunError("protokoll_unvollstaendig")
                if any(e["call_id"] == call_id for e in eintraege):
                    raise RunError("protokoll_unvollstaendig")
                gelesen = {
                    z["pfad"]
                    for z in lesezugriff(name, args, str(cwd))
                    if z["werkzeug"] != "bash"
                }
                event = {
                    "seq": len(eintraege),
                    "call_id": call_id,
                    "phase": phase,
                    "name": name,
                    "start": datetime.now(timezone.utc).isoformat(),
                    "dauer_ms": None,
                    "status": "abgebrochen",
                    "eingabe_sha256": sha(canonical(args)),
                    "ausgabe_sha256": None,
                    "material_ids": [
                        m.id
                        for m in material
                        if any(p in (".", m.ziel) or m.ziel.startswith(p + "/") for p in gelesen)
                    ],
                    "artefakt_ids": [],
                    "fehler_code": None,
                }
                path = args.get("file_path") if isinstance(args, dict) else None
                if name in ("Write", "Edit") and isinstance(path, str):
                    try:
                        relative = (cwd / path).resolve().relative_to(cwd.resolve()).as_posix()
                        event["artefakt_ids"] = [sha(relative.encode())]
                    except ValueError:
                        pass  # Outside the run folder: the place hook refuses it.
                eintraege.append(event)
                offen[call_id] = (event, time.monotonic())
            elif line.get("type") == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if call_id not in offen:
                    raise RunError("protokoll_unvollstaendig")
                event, started = offen.pop(call_id)
                event.update(
                    dauer_ms=int((time.monotonic() - started) * 1000),
                    status="error" if block.get("is_error") else "ok",
                    ausgabe_sha256=sha(canonical(block.get("content"))),
                    fehler_code="werkzeug_fehler" if block.get("is_error") else None,
                )
    return eintraege, not offen


def ergebnisfehler(ergebnis: dict | None) -> RunError | None:
    """Map the CLI's own run end onto contract codes; None for a finished run."""
    if ergebnis is None:
        return RunError("lauf_unvollstaendig")
    if ergebnis.get("subtype") == "error_max_turns":
        return RunError("lauf_unvollstaendig")
    status = ergebnis.get("api_error_status")
    if status in KAPAZITAET_STATUS:
        return RunError("kapazitaet_belegt", retryable=True, retry_after_s=60)
    if ergebnis.get("is_error") or ergebnis.get("subtype") != "success":
        return RunError("anbieter_fehler" if status is not None else "sandbox_fehler")
    return None


class CliLauf:
    """Adapter registered via Adapters.register_cli; never a bounded model."""

    hard_token_bound = False
    tools_are_requests = False
    eigene_schleife = True
    erklaerte_grenzen = (
        "max_turns",
        "max_tokens_je_antwort",
        "zeit_s",
        "eingang_vor_start",
        "laufende_nutzung",
    )

    def __init__(self, platz, *, cli_modell: str, zugang, zaehler):
        # zugang() returns the run credential; zaehler(model, system, prompt)
        # the provider-counted start input. Both server-owned, never request data.
        self.platz = platz
        self.cli_modell = cli_modell
        self.zugang = zugang
        self.zaehler = zaehler

    def werkzeuge(self, tools) -> list[str]:
        names = []
        for tool in tools:
            if tool.name not in CLI_WERKZEUGE or tool.parameter != {}:
                raise RunError("faehigkeit_nicht_verfuegbar")
            if tool.name in CLI_NUR_IM_PLATZ and not self.platz.dedicated_uid:
                raise RunError("faehigkeit_nicht_verfuegbar")
            names += [n for n in CLI_WERKZEUGE[tool.name] if n not in names]
        return names


def grenze_auskunft(run, turns, max_tokens):
    return {
        "hard_token_bound": False,
        "durchsetzung": "cli_laufend_gemessen",
        "tokens_gesamt": run.grenzen.tokens_gesamt,
        "zeit_s": run.grenzen.zeit_s,
        "max_turns": turns,
        "max_tokens_je_antwort": max_tokens,
        "eingang_gezaehlt": None,
        "tokens_verbraucht": 0,
        "tokens_reserviert_max": 0,
        "modellaufrufe": 0,
        "ursache": None,
    }


async def cli_phase(
    runtime,
    provider: CliLauf,
    request,
    tenant,
    state,
    directory,
    result,
    material,
    instruction,
    systemtext,
    model,
    tools,
    output,
    turns,
    max_tokens,
    budget,
    phase,
    main_result=None,
):
    """One phase on the CLI place, same result contract as Runtime.phase."""
    from src.claude_cli import antwort_abgeschnitten, antwort_ende

    run = request.agentischer_lauf
    grenze = result["grenze"] = grenze_auskunft(run, turns, max_tokens)
    if phase == "pruefung":
        # One phase on the CLI makes the whole run not hard-bounded.
        state["result"]["grenze"].update(
            hard_token_bound=False, durchsetzung="cli_laufend_gemessen"
        )
    zaehler = StromZaehler(budget[0], max_tokens)
    try:
        # The instruction stays first and unchanged; result and paths follow
        # as technical metadata in their own paragraphs.
        teile = [instruction]
        if main_result is not None:
            teile.append(canonical(main_result).decode())
        teile.append(
            canonical(
                {
                    "material_wurzel": "../eingang",
                    "material": sorted(material),
                    "ausgabe": output.model_dump(exclude_none=True),
                }
            ).decode()
        )
        prompt = "\n\n".join(teile)
        eingang = await provider.zaehler(provider.cli_modell, systemtext, prompt)
        if type(eingang) is not int or eingang < 0:
            raise RunError("faehigkeit_nicht_verfuegbar")
        grenze["eingang_gezaehlt"] = eingang
        grenze["tokens_reserviert_max"] = eingang + max_tokens
        if eingang + max_tokens > budget[0]:
            raise Grenze("grenze_tokens", "eingang_ueber_budget")
        zugang = provider.zugang()
        if not isinstance(zugang, str) or not zugang:
            raise RunError("faehigkeit_nicht_verfuegbar")
        attempt = state.get("attempt", 1)
        auftrag = CliAuftrag(
            schluessel=sha(canonical([tenant, run.lauf_id, phase, attempt, time.time_ns()])),
            prompt=prompt,
            systemtext=systemtext,
            model=provider.cli_modell,
            max_turns=turns,
            max_tokens=max_tokens,
            werkzeuge=provider.werkzeuge(tools),
            material=material,
            ausgaben=output.artefakte + ([output.datei] if output.datei else []),
            zeit_s=max(0.0, state["accepted_at"] + run.grenzen.zeit_s - time.time()),
            zugang=zugang,
        )
        zeilen: list[dict] = []

        def beobachten(line):
            zeilen.append(line)
            try:
                zaehler.zeile(line)
            finally:
                grenze["tokens_verbraucht"] = zaehler.verbraucht()
                grenze["modellaufrufe"] = len(zaehler.antworten)

        try:
            ausgang = await provider.platz.lauf(auftrag, beobachten)
        finally:
            budget[0] -= zaehler.verbraucht()
            state["transcript"].append({"phase": phase, "cli_zeilen": len(zeilen)})
        cwd = provider.platz.root / auftrag.schluessel / "lauf"
        eintraege, vollstaendig = werkzeugprotokoll(zeilen, phase, cwd, run.material)
        result["werkzeugaufrufe"] = {
            "vollstaendig": vollstaendig,
            "eintraege": eintraege,
            "protokoll_ref": None,
        }
        fehler = ergebnisfehler(zaehler.ergebnis)
        if fehler is not None:
            raise fehler
        if not vollstaendig:
            raise RunError("protokoll_unvollstaendig")
        gesamt = zaehler.abschluss()
        budget[0] -= sum(gesamt.values()) - zaehler.verbraucht()
        grenze["tokens_verbraucht"] = sum(gesamt.values())
        result["usage"] = {**gesamt, "cost_eur": None, "cost_source": "unknown"}
        chunks = chunks_aus_strom(zeilen)
        end = result["antwort_ende"] = antwort_ende(chunks)
        if antwort_abgeschnitten(end) and output.modus != "datei":
            raise RunError("antwort_abgeschnitten")
        if end is None:
            raise RunError("ende_unbekannt")
        if end != "end_turn":
            raise RunError("lauf_unvollstaendig")
        if output.modus == "datei":
            if output.datei not in ausgang.dateien:
                raise RunError("antwort_fehlt")
            content = ausgang.dateien[output.datei].decode()
        else:
            content = zaehler.ergebnis.get("result")
        if not isinstance(content, str) or not content.strip():
            raise RunError("antwort_fehlt")
        if zugang in content:
            raise RunError("sandbox_fehler")
        if output.modus == "json":
            try:
                parsed = json.loads(
                    content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                )
                Draft202012Validator(output.json_schema).validate(parsed)
            except Exception:
                raise RunError("ausgabe_ungueltig") from None
        blobs = {}
        for target in output.artefakte:
            matches = {
                p: b for p, b in ausgang.dateien.items() if p == target or p.startswith(target + "/")
            }
            if not matches:
                raise RunError("antwort_fehlt")
            blobs.update(matches)
        if output.datei:
            blobs[output.datei] = ausgang.dateien[output.datei]
        runtime.publish(request, tenant, state, result, phase, blobs)
        result["herkunft"]["antwort"] = {
            "id": "antwort",
            "pfad": output.datei or "antwort",
            "sha256": sha(content.encode()),
            "bytes": len(content.encode()),
        }
        result.update(
            status="success",
            content=content,
            antwort_quelle="datei" if output.modus == "datei" else "text",
        )
    except RunError as exc:
        if isinstance(exc, Grenze):
            grenze["ursache"] = exc.ursache
        runtime.fail(result, exc)
    finally:
        if usage_bekannt(result) is False and zaehler.antworten:
            # A stopped run still reports what the stream showed it used.
            result["usage"] = {
                **{k: sum(a[k] for a in zaehler.antworten.values()) for k in TOKEN_KEYS},
                "cost_eur": None,
                "cost_source": "unknown",
            }
        runtime.storage.save(directory, state)


def usage_bekannt(result) -> bool:
    return result["usage"].get("input_tokens") is not None
