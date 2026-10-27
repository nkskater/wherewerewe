import json

import httpx
import pytest
from pydantic import BaseModel

from wherewerewe.llm.client import (
    LLMClient,
    LLMError,
    _ThinkFilter,
    chat_json,
    extract_json,
    inline_refs,
    require_all,
)
from wherewerewe.llm.prompts import EpisodeSummaryOut


def test_extract_json_handles_fences_thinking_and_chatter():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('<think>hmm {"no": 1}</think>```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Sure! Here it is: {"a": 3} hope that helps') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_inline_refs_flattens_defs():
    schema = inline_refs(EpisodeSummaryOut.model_json_schema())
    assert "$defs" not in json.dumps(schema)
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["characters"]["items"]["properties"]["name"]["type"] == "string"


def test_require_all_marks_defaulted_fields_required():
    schema = require_all(inline_refs(EpisodeSummaryOut.model_json_schema()))
    assert set(schema["required"]) == set(schema["properties"])
    character = schema["properties"]["characters"]["items"]
    assert "relationships" in character["required"]


def test_think_filter_across_chunk_boundaries():
    f = _ThinkFilter()
    pieces = ["Hel", "lo <th", "ink>secret", " stuff</thi", "nk> world", " a < b"]
    assert "".join(f.feed(p) for p in pieces) == "Hello  world a < b"


def _ollama_transport(requests: list):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m:latest"}]})
        body = json.loads(request.content)
        if body["stream"]:
            lines = [json.dumps({"message": {"content": c}}) for c in ["Prev", "iously"]]
            return httpx.Response(200, text="\n".join(lines))
        return httpx.Response(200, json={"message": {"content": '{"a": 1}'}})

    return httpx.MockTransport(handler)


def test_ollama_native_mode_sends_context_and_schema():
    requests: list[httpx.Request] = []
    client = LLMClient(
        "http://ollama:11434/v1", "m", context_tokens=12345, transport=_ollama_transport(requests)
    )
    assert client.chat([{"role": "user", "content": "hi"}], schema={"type": "object"}) == '{"a": 1}'
    body = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/api/chat"
    assert body["options"]["num_ctx"] == 12345
    assert body["format"] == {"type": "object"}
    assert body["think"] is False
    assert "".join(client.stream([{"role": "user", "content": "x"}])) == "Previously"
    assert client.list_models() == ["m:latest"]


def test_ollama_retries_without_think_when_rejected():
    calls = []

    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        body = json.loads(request.content)
        calls.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "model does not support think"})
        return httpx.Response(200, json={"message": {"content": "fine"}})

    client = LLMClient("http://o:11434", "m", transport=httpx.MockTransport(handler))
    assert client.chat([{"role": "user", "content": "hi"}]) == "fine"
    assert "think" in calls[0] and "think" not in calls[1]


def test_openai_mode_falls_back_when_json_schema_unsupported():
    seen = []

    def handler(request):
        if request.url.path.endswith("/api/version"):
            return httpx.Response(404)
        body = json.loads(request.content)
        seen.append(body.get("response_format", {}).get("type"))
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": "response_format json_schema unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 5}'}}]})

    client = LLMClient(
        "https://api.example.com/v1", "gpt", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert client.api == "openai"
    assert client.chat([{"role": "user", "content": "x"}], schema={"type": "object"}) == '{"a": 5}'
    assert seen == ["json_schema", "json_object"]


def test_openai_stream_parses_sse():
    def handler(request):
        events = [
            {"choices": [{"delta": {"content": "Pre"}}]},
            {"choices": [{"delta": {"content": "viously"}}]},
        ]
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text)

    client = LLMClient("https://x/v1", "m", api="openai", transport=httpx.MockTransport(handler))
    assert "".join(client.stream([{"role": "user", "content": "x"}])) == "Previously"


def test_errors_are_wrapped():
    client = LLMClient(
        "https://x/v1",
        "m",
        api="openai",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "x"}])


class Out(BaseModel):
    a: int


class FlakyLLM:
    model = "m"
    context_tokens = 4096

    def __init__(self):
        self.replies = ["not json", '{"a": 7}']
        self.seen = []

    def chat(self, messages, *, schema=None, max_tokens=None):
        self.seen.append(messages)
        return self.replies.pop(0)


def test_chat_json_reasks_with_error():
    llm = FlakyLLM()
    assert chat_json(llm, [{"role": "user", "content": "x"}], Out).a == 7
    assert "not valid JSON" in llm.seen[1][-1]["content"]
