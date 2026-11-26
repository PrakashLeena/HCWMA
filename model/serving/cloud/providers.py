"""Cloud model backends over plain HTTPS with httpx (no cloud SDKs, owner decision 2026-11-26).

Each backend turns HCWMA's runtime-neutral `Message` list and Ollama-style tool schemas into
the provider's request, and the reply back into a `GenerateResult`. Tool calls are only
*proposed* and are validated into the same Pydantic `ToolCall` as the local model's; the
policy layer decides what runs. The API key goes only into a request header and is never
logged or put into an error message.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from model.serving.types import GenerateResult, Message, ToolCall


class CloudError(RuntimeError):
    """A cloud call failed. `kind` says why; the message never contains the key or prompt."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # network | timeout | rate_limit | auth | server | bad_request | invalid


def _error_for(resp: httpx.Response) -> CloudError:
    code = resp.status_code
    if code == 429:
        return CloudError("rate_limit", "the provider's rate limit was reached (HTTP 429)")
    if code in (401, 403):
        return CloudError("auth", f"the provider refused the API key (HTTP {code})")
    if code >= 500:
        return CloudError("server", f"the provider had an error (HTTP {code})")
    return CloudError("bad_request", f"the provider refused the request (HTTP {code})")


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "\n\nReply with only one JSON object (no code fence, no other text) that matches "
        f"this JSON schema:\n{json.dumps(schema, separators=(',', ':'))}"
    )


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def extract_json(text: str) -> str:
    """A JSON reply without a code fence or words around it (the Router's JSON check is strict)."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if stripped.startswith("{"):
        return stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if 0 <= start < end else stripped


def _args(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CloudError("invalid", "the cloud model's tool arguments were not JSON") from exc
    if not isinstance(raw, dict):
        raise CloudError("invalid", "tool call arguments must be a JSON object")
    return raw


def _tool_fn(tool: dict[str, Any]) -> dict[str, Any]:
    """Ollama/OpenAI style {"type": "function", "function": {...}} -> the inner function."""
    return tool.get("function", tool)


class _HttpBackend:
    provider = "cloud"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not model:
            raise ValueError("no cloud model name is set")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self._timeout = timeout_s
        self._transport = transport

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(model={self.model!r}, base_url={self.base_url!r})"

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise CloudError("timeout", "the provider did not answer in time") from None
        except httpx.HTTPError as exc:
            raise CloudError(
                "network", f"could not reach the provider ({type(exc).__name__})"
            ) from None
        if resp.status_code != 200:
            raise _error_for(resp)
        try:
            data = resp.json()
        except ValueError:
            raise CloudError("invalid", "the provider's reply was not JSON") from None
        if not isinstance(data, dict):
            raise CloudError("invalid", "the provider's reply was not a JSON object")
        return data


class OpenAICompatBackend(_HttpBackend):
    """OpenAI Chat Completions shape: OpenAI, Groq, OpenRouter, DeepSeek and others."""

    provider = "openai_compat"

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> GenerateResult:
        out = _openai_messages(messages)
        payload: dict[str, Any] = {"model": self.model, "messages": out, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = [{"type": "function", "function": _tool_fn(t)} for t in tools]
        if response_format is not None:
            _add_to_system(out, _schema_instruction(response_format))
            payload["response_format"] = {"type": "json_object"}
        if temperature is not None:
            payload["temperature"] = temperature
        if seed is not None:
            payload["seed"] = seed
        start = time.perf_counter()
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self._key}"},
        )
        total = time.perf_counter() - start
        choices = data.get("choices") or []
        if not choices:
            raise CloudError("invalid", "the provider's reply had no choices")
        msg = choices[0].get("message") or {}
        calls = [
            ToolCall(
                name=str((tc.get("function") or {}).get("name", "")),
                arguments=_args((tc.get("function") or {}).get("arguments")),
            )
            for tc in msg.get("tool_calls") or []
        ]
        text = msg.get("content") or ""
        if response_format is not None:
            text = extract_json(text)
        usage = data.get("usage") or {}
        return GenerateResult(
            text=text,
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_s=total,
            eval_s=total,
            provider=self.provider,
            model=self.model,
        )


def _add_to_system(out: list[dict[str, Any]], extra: str) -> None:
    if out and out[0].get("role") == "system":
        out[0]["content"] = (out[0].get("content") or "") + extra
    else:
        out.insert(0, {"role": "system", "content": extra.strip()})


def _openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Tool results need the id of the call they answer: ids are made up in order."""
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []  # (id, tool name) of calls not answered yet
    counter = 0
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            calls = []
            pending = []
            for c in m.tool_calls:
                counter += 1
                cid = f"call_{counter}"
                pending.append((cid, c.name))
                calls.append(
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                )
            out.append({"role": "assistant", "content": m.content or None, "tool_calls": calls})
        elif m.role == "tool":
            cid = _take(pending, m.tool_name)
            if cid is None:  # no call to answer: pass it on as plain text
                out.append({"role": "user", "content": f"[tool result]\n{m.content}"})
            else:
                out.append({"role": "tool", "tool_call_id": cid, "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _take(pending: list[tuple[str, str]], name: str | None) -> str | None:
    for i, (cid, tool) in enumerate(pending):
        if name is None or tool == name:
            pending.pop(i)
            return cid
    return pending.pop(0)[0] if pending else None


class AnthropicBackend(_HttpBackend):
    """Anthropic Messages API (`/v1/messages`)."""

    provider = "anthropic"
    API_VERSION = "2023-06-01"

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        seed: int | None = None,  # noqa: ARG002 - not supported by this API
    ) -> GenerateResult:
        system, out = _anthropic_messages(messages)
        if response_format is not None:
            system += _schema_instruction(response_format)
        payload: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": out}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": _tool_fn(t)["name"],
                    "description": _tool_fn(t).get("description", ""),
                    "input_schema": _tool_fn(t).get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        if temperature is not None:
            payload["temperature"] = temperature
        start = time.perf_counter()
        data = self._post(
            f"{self.base_url}/v1/messages",
            payload,
            {"x-api-key": self._key, "anthropic-version": self.API_VERSION},
        )
        total = time.perf_counter() - start
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(name=str(block.get("name", "")), arguments=_args(block.get("input")))
                )
        text = "".join(texts)
        if response_format is not None:
            text = extract_json(text)
        usage = data.get("usage") or {}
        return GenerateResult(
            text=text,
            tool_calls=calls,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            total_s=total,
            eval_s=total,
            provider=self.provider,
            model=self.model,
        )


def _anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []
    counter = 0

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        # The API wants user/assistant turns to alternate: merge neighbours.
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            pending = []
            for c in m.tool_calls or []:
                counter += 1
                cid = f"toolu_{counter}"
                pending.append((cid, c.name))
                blocks.append({"type": "tool_use", "id": cid, "name": c.name, "input": c.arguments})
            if blocks:
                add("assistant", blocks)
        elif m.role == "tool":
            cid = _take(pending, m.tool_name)
            if cid is None:
                add("user", [{"type": "text", "text": f"[tool result]\n{m.content}"}])
            else:
                add("user", [{"type": "tool_result", "tool_use_id": cid, "content": m.content}])
        else:
            add("user", [{"type": "text", "text": m.content}])
    return "\n\n".join(system_parts), out


class GeminiBackend(_HttpBackend):
    """Google Gemini `generateContent` (v1beta)."""

    provider = "gemini"

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> GenerateResult:
        system, contents = _gemini_contents(messages)
        if response_format is not None:
            system += _schema_instruction(response_format)
        config: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            config["temperature"] = temperature
        if seed is not None:
            config["seed"] = seed
        if response_format is not None and not tools:
            config["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": _tool_fn(t)["name"],
                            "description": _tool_fn(t).get("description", ""),
                            "parameters": _gemini_schema(
                                _tool_fn(t).get("parameters")
                                or {"type": "object", "properties": {}}
                            ),
                        }
                        for t in tools
                    ]
                }
            ]
        start = time.perf_counter()
        data = self._post(
            f"{self.base_url}/v1beta/models/{self.model}:generateContent",
            payload,
            {"x-goog-api-key": self._key},
        )
        total = time.perf_counter() - start
        candidates = data.get("candidates") or []
        if not candidates:
            raise CloudError("invalid", "the provider's reply had no candidates")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for part in (candidates[0].get("content") or {}).get("parts") or []:
            if "functionCall" in part:
                fc = part["functionCall"] or {}
                calls.append(
                    ToolCall(name=str(fc.get("name", "")), arguments=_args(fc.get("args")))
                )
            elif "text" in part and not part.get("thought"):
                texts.append(part.get("text") or "")
        text = "".join(texts)
        if response_format is not None:
            text = extract_json(text)
        usage = data.get("usageMetadata") or {}
        return GenerateResult(
            text=text,
            tool_calls=calls,
            prompt_tokens=int(usage.get("promptTokenCount") or 0),
            completion_tokens=int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0),
            total_s=total,
            eval_s=total,
            provider=self.provider,
            model=self.model,
        )


# JSON-schema keys Gemini's function declarations accept (an OpenAPI subset).
_GEMINI_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "properties",
    "required",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "anyOf",
    "propertyOrdering",
    "default",
    "title",
}


def _gemini_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _gemini_schema(v) for k, v in value.items()}
        elif key == "type" and isinstance(value, list):
            kinds = [v for v in value if v != "null"]
            out[key] = kinds[0] if kinds else "string"
            if "null" in value:
                out["nullable"] = True
        else:
            out[key] = _gemini_schema(value)
    return out


def _gemini_contents(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def add(role: str, parts: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})

    last_call: str | None = None
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for c in m.tool_calls or []:
                parts.append({"functionCall": {"name": c.name, "args": c.arguments}})
                last_call = c.name
            if parts:
                add("model", parts)
        elif m.role == "tool":
            name = m.tool_name or last_call or "tool"
            add("user", [{"functionResponse": {"name": name, "response": {"result": m.content}}}])
        else:
            add("user", [{"text": m.content}])
    return "\n\n".join(system_parts), out


def make_cloud_backend(
    kind: str,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout_s: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> _HttpBackend:
    classes: dict[str, type[_HttpBackend]] = {
        "openai_compat": OpenAICompatBackend,
        "anthropic": AnthropicBackend,
        "gemini": GeminiBackend,
    }
    if kind not in classes:
        raise ValueError(f"unknown cloud provider {kind!r}")
    return classes[kind](
        base_url=base_url, model=model, api_key=api_key, timeout_s=timeout_s, transport=transport
    )


def fetch_models(
    kind: str,
    *,
    base_url: str,
    api_key: str,
    timeout_s: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """The provider's model list (GET .../models). Uses no tokens; the key is a header only."""
    base = base_url.rstrip("/")
    if kind == "anthropic":
        url, headers = (
            f"{base}/v1/models?limit=1000",
            {
                "x-api-key": api_key,
                "anthropic-version": AnthropicBackend.API_VERSION,
            },
        )
    elif kind == "gemini":
        url, headers = f"{base}/v1beta/models?pageSize=1000", {"x-goog-api-key": api_key}
    else:
        url, headers = f"{base}/models", {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False) as c:
            resp = c.get(url, headers=headers)
    except httpx.TimeoutException:
        raise CloudError("timeout", "the provider did not answer in time") from None
    except httpx.HTTPError as exc:
        raise CloudError(
            "network", f"could not reach the provider ({type(exc).__name__})"
        ) from None
    if resp.status_code != 200:
        raise _error_for(resp)
    try:
        data = resp.json()
    except ValueError:
        raise CloudError("invalid", "the provider's model list was not JSON") from None
    ids: list[str] = []
    if kind == "gemini":
        for m in data.get("models") or []:
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                ids.append(str(m.get("name", "")).removeprefix("models/"))
    else:
        ids = [str(m.get("id", "")) for m in data.get("data") or [] if isinstance(m, dict)]
    return sorted({i for i in ids if i})
