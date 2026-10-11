"""General bounded tool loop and one independent app-specified review."""

from __future__ import annotations

import asyncio
import base64
import copy
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

from .adapter import Adapters, RunError, TOKEN_KEYS
from .sandbox import Sandbox, canonical, sha
from .storage import Storage


@asynccontextmanager
async def deadline_scope(seconds):
    task = asyncio.current_task()
    expired = False

    def cancel():
        nonlocal expired
        expired = True
        task.cancel()

    handle = asyncio.get_running_loop().call_later(seconds, cancel)
    try:
        yield
    except asyncio.CancelledError:
        if expired:
            raise TimeoutError() from None
        raise
    finally:
        handle.cancel()


def identity(name, path, data):
    return {"id": name, "pfad": path, "sha256": sha(data), "bytes": len(data)}


def request_digest(request):
    data = request.model_dump(mode="json", exclude_unset=True)
    data["agentischer_lauf"] = request.agentischer_lauf.model_dump(mode="json", by_alias=True)
    for item in data["agentischer_lauf"]["material"]:
        item.pop("quelle")
    # Explicit/implicit defaults of legacy fields must not alter this mode.
    return sha(
        canonical(
            {
                k: data[k]
                for k in (
                    "query",
                    "model",
                    "max_turns",
                    "max_tokens",
                    "antwort_im_text",
                    "agentischer_lauf",
                )
            }
        )
    )


def unknown_usage():
    return {**{k: None for k in TOKEN_KEYS}, "cost_eur": None, "cost_source": "unknown"}


def aggregate(usages):
    result = unknown_usage()
    for key in TOKEN_KEYS + ("cost_eur",):
        values = [u.get(key) for u in usages]
        result[key] = sum(values) if values and all(v is not None for v in values) else None
    if result["cost_eur"] is not None:
        result["cost_source"] = "adapter_aggregate"
    return result


class Runtime:
    def __init__(
        self,
        adapters: Adapters,
        storage: Storage,
        *,
        revision: str,
        max_bytes: int,
        material_resolver=None,
        bash=None,
    ):
        self.bash = bash
        self.adapters = adapters
        self.storage = storage
        self.revision = revision
        self.max_bytes = max_bytes
        # Resolver is server-owned. It must authorize every destination and
        # redirect, enforce network policy and bound the download while reading.
        self.material_resolver = material_resolver

    def preflight(self, request):
        run = request.agentischer_lauf
        for model, tools in [(request.model, run.werkzeuge)] + (
            [(run.pruefung.model, run.pruefung.werkzeuge)] if run.pruefung else []
        ):
            self.adapters.require(model)
            Sandbox.validate_tools(tools)
            if any(t.name == "Bash" for t in tools) and self.bash is None:
                raise RunError("faehigkeit_nicht_verfuegbar")
        if sum(m.bytes for m in run.material) > self.max_bytes:
            raise RunError("auftrag_ungueltig")
        if any(m.quelle.art == "verweis" for m in run.material) and self.material_resolver is None:
            raise RunError("faehigkeit_nicht_verfuegbar")

    def empty_result(self, request, tenant, digest, model, phase):
        run = request.agentischer_lauf
        return {
            "status": "error",
            "content": None,
            "error": None,
            "query": request.query if phase == "hauptlauf" else run.pruefung.auftrag,
            "model": model,
            "antwort_quelle": None,
            "antwort_ende": None,
            "fehler_code": None,
            "retryable": False,
            "retry_after_s": None,
            "herkunft": {
                "schema": "lauf-herkunft/1",
                "lauf_id": run.lauf_id,
                "auftrag_sha256": digest,
                "bridge_revision": self.revision,
                "model": model,
                "material": [
                    {"id": m.id, "pfad": m.ziel, "sha256": m.sha256, "bytes": m.bytes}
                    for m in run.material
                ],
                "antwort": None,
                "artefakte": [],
                "cache": {"hit": False, "ursprung_lauf_id": None},
            },
            "usage": unknown_usage(),
            "werkzeugaufrufe": {"vollstaendig": False, "eintraege": [], "protokoll_ref": None},
            "artefakte": [],
            "pruefung": None,
            "diagnose_ref": self.storage.reference(tenant, run.lauf_id, phase, "diagnose"),
        }

    @staticmethod
    def retryable_state(state):
        # A released but incomplete checkpoint is uncertain, never retryable.
        return bool(
            state
            and state["stage"] == "finished"
            and state["result"]["status"] == "error"
            and state["result"].get("retryable") is True
        )

    def next_attempt(self, directory, state):
        if not self.retryable_state(state):
            return state.get("attempt", 1) if state else 1
        attempt = state.get("attempt", 1)
        self.storage.archive(directory, attempt, state)
        return attempt + 1

    def reserve(self, request, tenant):
        """Checkpoint acceptance time before a durable job can be deferred."""
        run = request.agentischer_lauf
        digest = request_digest(request)
        with self.storage.lock(tenant, run.lauf_id) as directory:
            state = self.storage.load(directory)
            if state is not None:
                if state["digest"] != digest:
                    raise RunError("lauf_konflikt")
                if not self.retryable_state(state):
                    return
            attempt = self.next_attempt(directory, state)
            self.storage.save(
                directory,
                {
                    "attempt": attempt,
                    "digest": digest,
                    "accepted_at": time.time(),
                    "stage": "accepted",
                    "result": self.empty_result(
                        request, tenant, digest, request.model, "hauptlauf"
                    ),
                    "blobs": {},
                    "transcript": [],
                },
            )

    async def execute(self, request, tenant):
        self.preflight(request)
        run = request.agentischer_lauf
        digest = request_digest(request)
        with self.storage.lock(tenant, run.lauf_id) as directory:
            state = self.storage.load(directory)
            accepted = state if state and state["stage"] == "accepted" else None
            if state and state["digest"] != digest:
                raise RunError("lauf_konflikt")
            if state and not accepted and not self.retryable_state(state):
                if state["digest"] != digest:
                    raise RunError("lauf_konflikt")
                result = copy.deepcopy(state["result"])
                if state["stage"] != "finished":
                    result.update(
                        status="error",
                        content=None,
                        error="lauf_unvollstaendig",
                        fehler_code="lauf_unvollstaendig",
                    )
                    return result
                if result["status"] == "success":
                    result["herkunft"]["cache"] = {"hit": True, "ursprung_lauf_id": run.lauf_id}
                    # Original usage remains in the protected checkpoint, not re-billed.
                    result["usage"] = {
                        **{k: 0 for k in TOKEN_KEYS},
                        "cost_eur": 0,
                        "cost_source": "cache_no_charge",
                    }
                    if result.get("pruefung"):
                        result["pruefung"]["usage"] = copy.deepcopy(result["usage"])
                return result
            attempt = self.next_attempt(directory, state)
            result = self.empty_result(request, tenant, digest, request.model, "hauptlauf")
            state = {
                "attempt": attempt,
                "digest": digest,
                "accepted_at": accepted["accepted_at"] if accepted else time.time(),
                "stage": "started",
                "result": result,
                "blobs": {},
                "transcript": [],
            }
            self.storage.save(directory, state)
            deadline = state["accepted_at"] + run.grenzen.zeit_s
            budget = [run.grenzen.tokens_gesamt]
            material = {}
            try:
                if deadline <= time.time():
                    raise RunError("grenze_zeit")
                async with deadline_scope(max(0, deadline - time.time())):
                    for item in run.material:
                        if item.quelle.art == "inline":
                            data = base64.b64decode(item.quelle.content_base64, validate=True)
                        else:
                            data = await self.material_resolver(tenant, item, self.max_bytes)
                        if len(data) != item.bytes or sha(data) != item.sha256:
                            raise RunError("material_integritaet")
                        material[item.ziel] = data
                    await self.phase(
                        request,
                        tenant,
                        state,
                        directory,
                        result,
                        material,
                        request.query,
                        run.systemtext,
                        request.model,
                        run.werkzeuge,
                        run.ausgabe,
                        request.max_turns,
                        request.max_tokens,
                        budget,
                        "hauptlauf",
                    )
                    if result["status"] == "success" and run.pruefung:
                        review = run.pruefung
                        check = self.empty_result(request, tenant, digest, review.model, "pruefung")
                        result["pruefung"] = check
                        # Only declared artifacts cross the sandbox boundary.
                        given = dict(material)
                        for blob in state["blobs"].values():
                            if blob["phase"] == "hauptlauf":
                                path = blob["pfad"]
                                if path in given:
                                    raise RunError("artefakt_integritaet")
                                given[path] = base64.b64decode(blob["data"])
                        await self.phase(
                            request,
                            tenant,
                            state,
                            directory,
                            check,
                            given,
                            review.auftrag,
                            review.systemtext,
                            review.model,
                            review.werkzeuge,
                            review.ausgabe,
                            review.max_turns,
                            review.max_tokens,
                            budget,
                            "pruefung",
                            {"content": result["content"], "artefakte": result["artefakte"]},
                        )
                        result["usage"] = aggregate([result["usage"], check["usage"]])
                        if check["status"] != "success":
                            result.update(
                                status="error",
                                content=None,
                                error=check["error"],
                                fehler_code=check["fehler_code"],
                                retryable=check["retryable"],
                                retry_after_s=check["retry_after_s"],
                            )
            except TimeoutError:
                self.fail(result, RunError("grenze_zeit"))
            except RunError as exc:
                self.fail(result, exc)
            except Exception:
                # Raw provider/material exceptions can contain credentials or content.
                self.fail(result, RunError("sandbox_fehler"))
            state["stage"] = "finished"
            self.storage.save(directory, state)
            return copy.deepcopy(result)

    @staticmethod
    def fail(result, exc):
        result.update(
            status="error",
            content=None,
            error=exc.code,
            fehler_code=exc.code,
            retryable=exc.retryable,
            retry_after_s=exc.retry_after_s,
        )

    async def phase(
        self,
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
        # BRTRUNC remains the single authority on final-message end signals.
        from src.claude_cli import antwort_ende, antwort_abgeschnitten

        sandbox = Sandbox(
            material,
            tools,
            output.artefakte + ([output.datei] if output.datei else []),
            self.max_bytes,
            bash=self.bash,
        )
        messages = []
        if systemtext:
            messages.append({"role": "system", "content": systemtext})
        messages.append({"role": "user", "content": instruction})
        if main_result is not None:
            messages.append({"role": "user", "content": canonical(main_result).decode()})
        # Paths and output contract are technical metadata, never app instructions.
        messages.append(
            {
                "role": "user",
                "content": canonical(
                    {"material": sorted(material), "ausgabe": output.model_dump(exclude_none=True)}
                ).decode(),
            }
        )
        specs = [t.model_dump() for t in tools]
        provider = self.adapters.require(model)
        usages = []
        seen_calls = set()
        try:
            for turn in range(turns):
                bound = await provider.input_bound(model, copy.deepcopy(messages), specs)
                if type(bound) is not int or bound < 0:
                    raise RunError("faehigkeit_nicht_verfuegbar")
                cap = min(max_tokens, budget[0] - bound)
                if cap <= 0:
                    raise RunError("grenze_tokens")
                # No model invocation occurs without a prior reservation.
                reply = await provider.complete(
                    model=model, messages=copy.deepcopy(messages), tools=specs, max_tokens=cap
                )
                counters = [reply.usage.get(k) for k in TOKEN_KEYS]
                if any(type(v) is not int or v < 0 for v in counters):
                    raise RunError("grenze_tokens")
                if counters[1] > cap or sum(counters) - counters[1] > bound:
                    raise RunError("grenze_tokens")
                cost = reply.usage.get("cost_eur")
                if cost is not None and (
                    type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
                ):
                    raise RunError("protokoll_unvollstaendig")
                usages.append({**dict(zip(TOKEN_KEYS, counters)), "cost_eur": cost})
                result["usage"] = aggregate(usages)
                budget[0] -= sum(counters)
                result["antwort_ende"] = antwort_ende(reply.chunks)
                if not reply.complete_log:
                    raise RunError("protokoll_unvollstaendig")
                result["werkzeugaufrufe"]["vollstaendig"] = True
                # The adapter must expose every model-requested tool call.
                streamed = [
                    b
                    for c in reply.chunks
                    for b in c.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                if [(c.get("id"), c.get("name"), c.get("input")) for c in streamed] != [
                    (c.get("id"), c.get("name"), c.get("input")) for c in reply.calls
                ]:
                    result["werkzeugaufrufe"]["vollstaendig"] = False
                    raise RunError("protokoll_unvollstaendig")
                if not reply.calls:
                    end = result["antwort_ende"]
                    if antwort_abgeschnitten(end) and output.modus != "datei":
                        raise RunError("antwort_abgeschnitten")
                    if end is None:
                        raise RunError("ende_unbekannt")
                    if end != "end_turn":
                        raise RunError("lauf_unvollstaendig")
                    content = (
                        sandbox.read(output.datei).decode()
                        if output.modus == "datei"
                        else reply.text
                    )
                    if not isinstance(content, str) or not content.strip():
                        raise RunError("antwort_fehlt")
                    if output.modus == "json":
                        import json

                        try:
                            parsed = json.loads(
                                content,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                            )
                            Draft202012Validator(output.json_schema).validate(parsed)
                        except Exception:
                            raise RunError("ausgabe_ungueltig") from None
                    blobs = sandbox.artifacts(output.artefakte)
                    if output.datei:
                        blobs[output.datei] = sandbox.read(output.datei)
                    self.publish(request, tenant, state, result, phase, blobs)
                    result["herkunft"]["antwort"] = identity(
                        "antwort", output.datei or "antwort", content.encode()
                    )
                    result.update(
                        status="success",
                        content=content,
                        antwort_quelle="datei" if output.modus == "datei" else "text",
                    )
                    return
                messages.append(
                    {"role": "assistant", "content": reply.text, "tool_calls": reply.calls}
                )
                for call in reply.calls:
                    call_id, name, args = call.get("id"), call.get("name"), call.get("input")
                    if not isinstance(call_id, str) or not call_id or call_id in seen_calls:
                        raise RunError("protokoll_unvollstaendig")
                    seen_calls.add(call_id)
                    event = {
                        "seq": len(result["werkzeugaufrufe"]["eintraege"]),
                        "call_id": call_id,
                        "phase": phase,
                        "name": name,
                        "start": datetime.now(timezone.utc).isoformat(),
                        "dauer_ms": None,
                        "status": "abgebrochen",
                        "eingabe_sha256": sha(canonical(args)),
                        "ausgabe_sha256": None,
                        "material_ids": [],
                        "artefakt_ids": [],
                        "fehler_code": None,
                    }
                    result["werkzeugaufrufe"]["eintraege"].append(event)
                    started = time.monotonic()
                    try:
                        value = await sandbox.acall(name, args)
                    except RunError as exc:
                        event.update(status="error", fehler_code=exc.code)
                        raise
                    except Exception:
                        event.update(status="error", fehler_code="werkzeug_fehler")
                        raise RunError("werkzeug_fehler") from None
                    finally:
                        event["dauer_ms"] = int((time.monotonic() - started) * 1000)
                    event.update(status="ok", ausgabe_sha256=sha(canonical(value)))
                    event["material_ids"] = [
                        m.id
                        for m in request.agentischer_lauf.material
                        if args.get("path") == m.ziel and name == "Read"
                    ]
                    if name == "Bash":
                        # A computation returns a complete snapshot, including
                        # deletions. Do not resurrect a prior script in review/cache.
                        state["blobs"] = {
                            ref: blob
                            for ref, blob in state["blobs"].items()
                            if blob["phase"] != phase or blob["pfad"] in sandbox.files
                        }
                        result["artefakte"] = [
                            item for item in result["artefakte"] if item["pfad"] in sandbox.files
                        ]
                        event["artefakt_ids"] = [sha(p.encode()) for p in sorted(sandbox.files)]
                    if name == "Write":
                        event["artefakt_ids"] = [sha(args["path"].encode())]
                    self.publish(request, tenant, state, result, phase, sandbox.files)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": value})
                    state["transcript"].append({"phase": phase, "call": call, "result": value})
                self.storage.save(directory, state)
            raise RunError("lauf_unvollstaendig")
        except RunError as exc:
            self.fail(result, exc)

    def publish(self, request, tenant, state, result, phase, blobs):
        for path, data in blobs.items():
            item = identity(sha(path.encode()), path, data)
            ref = self.storage.reference(tenant, request.agentischer_lauf.lauf_id, phase, path)
            item["ref"] = ref
            result["artefakte"] = [a for a in result["artefakte"] if a["pfad"] != path] + [item]
            state["blobs"][ref] = {**item, "phase": phase, "data": base64.b64encode(data).decode()}
        result["herkunft"]["artefakte"] = [
            {k: v for k, v in item.items() if k != "ref"} for item in result["artefakte"]
        ]
