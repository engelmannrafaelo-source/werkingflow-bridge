"""ATOK: hard per-run token bound over the Bedrock Messages adapter.

Synthetic client only; no model call, no AWS access.
"""

import io
import json

import pytest
from botocore.exceptions import ClientError

from src.models import ResearchRequest, ResearchResponse
from src.agentischer_lauf.adapter import Adapters, RunError
from src.agentischer_lauf.bedrock import BedrockBoundedModel, body_for, to_anthropic
from src.agentischer_lauf.runtime import Runtime
from src.agentischer_lauf.storage import Storage
from .test_contract import payload, material


class Meta:
    def __init__(self, retries):
        self.config = type("C", (), {"retries": retries})()


class FakeBedrock:
    """Counts tokens as one per byte of the body; answers from a script.

    Each scripted answer is a function (body) -> (content, stop_reason, usage);
    usage None means: input = counted, output = the full max_tokens.
    """

    def __init__(self, answers, *, retries=None, count=True, count_value=None):
        self.meta = Meta(retries or {"total_max_attempts": 1, "mode": "standard"})
        self.answers = list(answers)
        self.log = []
        self.count_value = count_value
        if not count:
            self.count_tokens = None

    @staticmethod
    def tokens(body):
        rest = {k: v for k, v in body.items() if k != "max_tokens"}
        return len(json.dumps(rest)) // 10

    def count_tokens(self, *, modelId, input):
        body = json.loads(input["invokeModel"]["body"])
        self.log.append(("count", body))
        if self.count_value is not None:
            return {"inputTokens": self.count_value}
        return {"inputTokens": self.tokens(body)}

    def invoke_model(self, *, modelId, body, **_):
        body = json.loads(body)
        self.log.append(("invoke", body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        content, stop, usage = answer(body)
        if usage is None:
            usage = {"input_tokens": self.tokens(body), "output_tokens": body["max_tokens"]}
        data = {"content": content, "stop_reason": stop, "usage": usage}
        return {
            "body": io.BytesIO(json.dumps(data).encode()),
            "ResponseMetadata": {"RequestId": f"req-{len(self.log)}"},
        }


def text(value="alpha", usage=None, stop="end_turn"):
    return lambda body: ([{"type": "text", "text": value}], stop, usage)


def tool(call_id="t1", path="notes/catalog.txt"):
    return lambda body: (
        [{"type": "tool_use", "id": call_id, "name": "Read", "input": {"path": path}}],
        "tool_use",
        None,
    )


def setup(tmp_path, client):
    booked = []

    async def book(**kwargs):
        booked.append(kwargs)

    adapters = Adapters()
    adapters.register(
        "synthetic", BedrockBoundedModel(client, "eu.synthetic-v1:0", region="eu-x", book=book)
    )
    rt = Runtime(adapters, Storage(tmp_path / "p"), revision="r", max_bytes=100000)
    return rt, booked


def request(tokens=500, max_tokens=40, turns=4, tools=True):
    body = payload(max_tokens=max_tokens, max_turns=turns)
    body["agentischer_lauf"]["grenzen"]["tokens_gesamt"] = tokens
    if tools:
        body["agentischer_lauf"]["werkzeuge"] = [{"name": "Read", "parameter": {}}]
        body["agentischer_lauf"]["material"] = [material()]
    return ResearchRequest(**body)


def invokes(client):
    return [b for kind, b in client.log if kind == "invoke"]


def test_flag_requires_count_tokens_and_single_attempt():
    book = lambda **_: None  # noqa: E731
    adapters = Adapters()
    for client in (FakeBedrock([], count=False), FakeBedrock([], retries={"mode": "legacy"})):
        model = BedrockBoundedModel(client, "m", region="r", book=book)
        assert model.hard_token_bound is False
        with pytest.raises(RunError) as exc:
            adapters.register("x", model)
        assert exc.value.code == "faehigkeit_nicht_verfuegbar"
    assert BedrockBoundedModel(FakeBedrock([]), "m", region="r", book=book).hard_token_bound


def test_booking_is_mandatory():
    with pytest.raises(TypeError):
        BedrockBoundedModel(FakeBedrock([]), "m", region="r", book=None)


async def test_every_call_counted_first_and_capped(tmp_path):
    client = FakeBedrock([tool(), text(usage={"input_tokens": 5, "output_tokens": 2})])
    rt, booked = setup(tmp_path, client)
    result = await rt.execute(request(tokens=10_000), "t")
    assert result["status"] == "success", result
    kinds = [k for k, _ in client.log]
    assert kinds == ["count", "invoke", "count", "invoke"]
    for (_, counted), (_, sent) in zip(client.log[::2], client.log[1::2]):
        assert {k: v for k, v in counted.items() if k != "max_tokens"} == {
            k: v for k, v in sent.items() if k != "max_tokens"
        }
        assert sent["max_tokens"] == 40
        assert "cache_control" not in json.dumps(sent) and "thinking" not in sent
    assert len(booked) == 2 and booked[0]["aws_request_id"] == "req-2"
    grenze = result["grenze"]
    assert grenze["modellaufrufe"] == 2 and grenze["durchsetzung"] == "vorab_reserviert"
    assert grenze["tokens_verbraucht"] == sum(
        result["usage"][k]
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )
    assert ResearchResponse(**result).grenze == grenze


async def test_cap_shrinks_to_remaining_budget(tmp_path):
    client = FakeBedrock([tool(), text()])
    rt, _ = setup(tmp_path, client)
    first = FakeBedrock.tokens(body_for(*_first_messages(), 1))
    budget = first + 40 + first + 60  # second call has a longer input
    result = await rt.execute(request(tokens=budget), "t")
    sent = invokes(client)
    assert sent[0]["max_tokens"] == 40
    second_input = FakeBedrock.tokens(sent[1])
    assert sent[1]["max_tokens"] == budget - (first + 40) - second_input
    assert result["grenze"]["tokens_verbraucht"] <= budget


def _first_messages():
    messages = [
        {"role": "system", "content": "App-eigener Wortlaut."},
        {"role": "user", "content": "Sortiere Titel eines Filmarchivs."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ausgabe": {"artefakte": [], "modus": "text"},
                    "material": ["notes/catalog.txt"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        },
    ]
    return messages, [{"name": "Read", "parameter": {}}]


async def test_exhausted_budget_sends_nothing(tmp_path):
    client = FakeBedrock([tool(), text()])
    rt, booked = setup(tmp_path, client)
    first = FakeBedrock.tokens(body_for(*_first_messages(), 1))
    result = await rt.execute(request(tokens=first + 40 + 1), "t")
    assert result["fehler_code"] == "grenze_tokens" and result["status"] == "error"
    assert result["grenze"]["ursache"] == "restbudget_erschoepft"
    assert len(invokes(client)) == 1 and len(booked) == 1
    assert result["grenze"]["tokens_verbraucht"] == first + 40


@pytest.mark.parametrize("budget", [1, 7, 30, 61, 99, 140, 200, 333, 517])
async def test_total_never_exceeds_budget(tmp_path, budget):
    # Worst case provider: every reservation fully used, model never stops.
    client = FakeBedrock([tool(f"t{i}") for i in range(20)])
    rt, booked = setup(tmp_path, client)
    result = await rt.execute(request(tokens=budget, max_tokens=25, turns=20), "t")
    used = sum(b["usage"]["input_tokens"] + b["usage"]["output_tokens"] for b in booked)
    assert used <= budget
    assert result["grenze"]["tokens_verbraucht"] == used
    assert result["status"] == "error"


@pytest.mark.parametrize(
    "usage,ursache",
    [
        ({"input_tokens": 10**6, "output_tokens": 1}, "eingabe_ueber_reservierung"),
        ({"input_tokens": 1, "output_tokens": 41}, "ausgabe_ueber_reservierung"),
        ({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 10**6}, None),
        ({"input_tokens": 1}, "nutzung_unbekannt"),
    ],
)
async def test_provider_overrun_is_loud_and_booked(tmp_path, usage, ursache):
    client = FakeBedrock([text(usage=usage)])
    rt, booked = setup(tmp_path, client)
    result = await rt.execute(request(tokens=10**7, tools=False), "t")
    assert result["status"] == "error" and result["fehler_code"] == "grenze_tokens"
    assert result["grenze"]["ursache"] == (ursache or "eingabe_ueber_reservierung")
    assert len(booked) == 1


async def test_uncounted_body_is_refused():
    client = FakeBedrock([text()])
    model = BedrockBoundedModel(client, "m", region="r", book=lambda **_: None)
    with pytest.raises(RunError) as exc:
        await model.complete(
            model="m", messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=5
        )
    assert exc.value.code == "faehigkeit_nicht_verfuegbar" and not invokes(client)


async def test_no_count_no_estimate(tmp_path):
    client = FakeBedrock([text()], count_value="12")
    rt, _ = setup(tmp_path, client)
    result = await rt.execute(request(tools=False), "t")
    assert result["fehler_code"] == "faehigkeit_nicht_verfuegbar"
    assert not invokes(client)


async def test_throttle_is_retryable_capacity(tmp_path):
    error = ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeModel")
    client = FakeBedrock([error])
    rt, booked = setup(tmp_path, client)
    result = await rt.execute(request(tools=False), "t")
    assert result["fehler_code"] == "kapazitaet_belegt" and result["retryable"] is True
    assert not booked


def test_tool_results_go_back_as_user_blocks():
    system, messages, specs = to_anthropic(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "1", "name": "List", "input": {}}
            ]},
            {"role": "tool", "tool_call_id": "1", "content": ["x"]},
        ],
        [{"name": "List", "parameter": {}}],
    )
    assert system == [{"type": "text", "text": "s"}]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == [{"type": "tool_result", "tool_use_id": "1", "content": '["x"]'}]
    assert specs[0]["name"] == "List"
    with pytest.raises(RunError):
        to_anthropic([], [{"name": "Bash", "parameter": {}}])
