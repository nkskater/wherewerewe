"""LLM client for OpenAI-compatible endpoints, with a native Ollama mode.

Ollama's OpenAI-compatible endpoint ignores the context-window option, so a
14-page episode transcript would be silently truncated to the model default.
When the endpoint is Ollama (auto-detected), the native /api/chat endpoint is used
instead, which accepts `num_ctx`, JSON-schema `format`, and `think: false`.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterator
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    model: str
    context_tokens: int

    def chat(
        self, messages: list[dict], *, schema: dict | None = None, max_tokens: int | None = None
    ) -> str: ...

    def stream(self, messages: list[dict], *, max_tokens: int | None = None) -> Iterator[str]: ...


_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text).strip()


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply that may include fences, thinking, or chatter."""
    text = strip_thinking(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("no JSON object in model output")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def inline_refs(schema: dict) -> dict:
    """Resolve $ref/$defs so grammar-constrained decoders see a flat schema."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[name]))
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def require_all(schema: dict) -> dict:
    """Mark every property required.

    Pydantic leaves fields with defaults out of "required", and grammar-constrained
    decoders (Ollama, llama.cpp) then let the model stop after the first field.
    """
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        api: str = "auto",
        context_tokens: int = 16384,
        temperature: float = 0.2,
        timeout: float = 900,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.context_tokens = context_tokens
        self.temperature = temperature
        self._api = api
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            headers=headers, timeout=httpx.Timeout(timeout, connect=10), transport=transport
        )
        self._send_think = True

    @property
    def root_url(self) -> str:
        return re.sub(r"/v1$", "", self.base_url)

    @property
    def api(self) -> str:
        if self._api == "auto":
            self._api = "openai"
            try:
                resp = self._http.get(f"{self.root_url}/api/version", timeout=5)
                if resp.status_code == 200 and "version" in resp.json():
                    self._api = "ollama"
            except (httpx.HTTPError, ValueError):
                pass
        return self._api

    # -- requests -----------------------------------------------------------------

    def _ollama_body(self, messages, schema, max_tokens, stream) -> dict:
        options: dict[str, Any] = {"num_ctx": self.context_tokens, "temperature": self.temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": options,
        }
        if self._send_think:
            body["think"] = False
        if schema is not None:
            body["format"] = inline_refs(schema)
        return body

    def _openai_body(self, messages, schema, max_tokens, stream, schema_mode="json_schema"):
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": stream,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if schema is not None and schema_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": inline_refs(schema)},
            }
        elif schema is not None and schema_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _post(self, url: str, body: dict) -> httpx.Response:
        try:
            resp = self._http.post(url, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        return resp

    def chat(
        self, messages: list[dict], *, schema: dict | None = None, max_tokens: int | None = None
    ) -> str:
        if self.api == "ollama":
            resp = self._post(
                f"{self.root_url}/api/chat", self._ollama_body(messages, schema, max_tokens, False)
            )
            if resp.status_code == 400 and "think" in resp.text and self._send_think:
                self._send_think = False
                return self.chat(messages, schema=schema, max_tokens=max_tokens)
            if resp.status_code != 200:
                raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:500]}")
            return strip_thinking(resp.json()["message"]["content"])

        modes = ["json_schema", "json_object", "none"] if schema is not None else ["none"]
        for mode in modes:
            resp = self._post(
                f"{self.base_url}/chat/completions",
                self._openai_body(messages, schema, max_tokens, False, mode),
            )
            if resp.status_code == 400 and mode != "none" and "response_format" in resp.text:
                continue  # endpoint doesn't support this structured-output mode; fall back
            if resp.status_code != 200:
                raise LLMError(f"LLM error {resp.status_code}: {resp.text[:500]}")
            content = resp.json()["choices"][0]["message"].get("content") or ""
            return strip_thinking(content)
        raise LLMError("no structured output mode accepted")  # pragma: no cover

    def stream(self, messages: list[dict], *, max_tokens: int | None = None) -> Iterator[str]:
        """Yield text deltas. <think> blocks are filtered out as they stream."""
        if self.api == "ollama":
            url = f"{self.root_url}/api/chat"
            body = self._ollama_body(messages, None, max_tokens, True)
        else:
            url = f"{self.base_url}/chat/completions"
            body = self._openai_body(messages, None, max_tokens, True)
        filt = _ThinkFilter()
        try:
            with self._http.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    resp.read()
                    if (
                        self.api == "ollama"
                        and resp.status_code == 400
                        and "think" in resp.text
                        and self._send_think
                    ):
                        self._send_think = False
                        yield from self.stream(messages, max_tokens=max_tokens)
                        return
                    raise LLMError(f"LLM error {resp.status_code}: {resp.text[:500]}")
                for line in resp.iter_lines():
                    piece = self._parse_stream_line(line)
                    if piece is None:
                        continue
                    out = filt.feed(piece)
                    if out:
                        yield out
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM stream failed: {exc}") from exc

    def _parse_stream_line(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        if self.api == "ollama":
            data = json.loads(line)
            if data.get("error"):
                raise LLMError(data["error"])
            return (data.get("message") or {}).get("content") or None
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if payload == "[DONE]":
            return None
        data = json.loads(payload)
        choices = data.get("choices") or []
        return (choices[0].get("delta") or {}).get("content") if choices else None

    # -- helpers ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        if self.api == "ollama":
            resp = self._http.get(f"{self.root_url}/api/tags", timeout=10)
            resp.raise_for_status()
            return sorted(m["name"] for m in resp.json().get("models", []))
        resp = self._http.get(f"{self.base_url}/models", timeout=10)
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))

    def ping(self) -> str:
        models = self.list_models()
        if self.model not in models and f"{self.model}:latest" not in models:
            return f"reachable ({self.api}), but model {self.model!r} is not available"
        return f"ok ({self.api}, model {self.model})"


class _ThinkFilter:
    """Drop <think>...</think> spans from a token stream."""

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = []
        while self.buf:
            if self.inside:
                end = self.buf.find("</think>")
                if end == -1:
                    self.buf = self.buf[-8:]
                    break
                self.buf = self.buf[end + 8 :]
                self.inside = False
            else:
                start = self.buf.find("<think>")
                if start == -1:
                    # hold back a possible partial "<think" at the end
                    cut = self.buf.rfind("<")
                    if cut != -1 and "<think>".startswith(self.buf[cut:]):
                        out.append(self.buf[:cut])
                        self.buf = self.buf[cut:]
                    else:
                        out.append(self.buf)
                        self.buf = ""
                    break
                out.append(self.buf[:start])
                self.buf = self.buf[start + 7 :]
                self.inside = True
        return "".join(out)


def chat_json[T: BaseModel](
    llm: LLM,
    messages: list[dict],
    model_cls: type[T],
    retries: int = 1,
    max_tokens: int | None = None,
) -> T:
    """Ask for JSON matching a pydantic model, re-asking once with the error if it doesn't."""
    schema = require_all(inline_refs(model_cls.model_json_schema()))
    convo = list(messages)
    last_error = ""
    for _attempt in range(retries + 1):
        raw = llm.chat(convo, schema=schema, max_tokens=max_tokens)
        try:
            return model_cls.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:800]
            log.warning("invalid JSON from model, retrying: %s", last_error)
            convo = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "That reply was not valid JSON for the schema "
                    f"({last_error}). Reply again with only the JSON object.",
                },
            ]
    raise LLMError(f"model did not return valid JSON: {last_error}")


def make_llm(settings: Settings, *, verify: bool = False) -> LLMClient:
    return LLMClient(
        settings.llm_base_url,
        settings.effective_verify_model if verify else settings.llm_model,
        api_key=settings.llm_api_key,
        api=settings.llm_api,
        context_tokens=settings.llm_context_tokens,
        temperature=0.0 if verify else settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        transport=httpx.HTTPTransport(verify=settings.verify_ssl),
    )


# Tests swap this out to inject a fake model.
llm_factory = make_llm


def get_llm(settings: Settings, *, verify: bool = False) -> LLM:
    return llm_factory(settings, verify=verify)
