"""Ollama runtime backend. Only `model/serving/` knows Ollama exists.

Talks to the local Ollama server over HTTP on 127.0.0.1 with the standard library.
The model only *proposes* tool calls; this module never executes them.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

from model.serving.config import LOCAL_HOSTS, ModelConfig
from model.serving.types import GenerateResult, Message, ToolCall

Opener = Callable[..., Any]

_NS = 1e9


class OllamaBackend:
    def __init__(self, config: ModelConfig, opener: Opener | None = None) -> None:
        self.config = config
        self._base_url = config.endpoint.base_url
        parsed = urlparse(self._base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
            raise ValueError(f"refusing non-local model endpoint {self._base_url!r}")
        self._open = opener or urllib.request.urlopen

    # -- low level ---------------------------------------------------------------------------

    def _post_lines(self, path: str, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        req = urllib.request.Request(  # noqa: S310 - URL checked to be http://127.0.0.1 above
            self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._open(req, timeout=self.config.request_timeout_s) as resp:
            for raw in resp:
                line = raw.strip()
                if line:
                    yield json.loads(line)

    def _get_json(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self._base_url + path, method="GET")  # noqa: S310
        with self._open(req, timeout=30) as resp:
            return json.loads(resp.read())

    # -- public ------------------------------------------------------------------------------

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> GenerateResult:
        options: dict[str, Any] = {
            "num_predict": max_tokens,
            "num_ctx": self.config.context_length,
        }
        if temperature is not None:
            options["temperature"] = temperature
        if seed is not None:
            options["seed"] = seed
        payload: dict[str, Any] = {
            "model": self.config.ollama_model,
            "messages": [_to_ollama(m) for m in messages],
            "stream": True,
            "think": self.config.think,
            "keep_alive": self.config.keep_alive,
            "options": options,
        }
        if tools:
            payload["tools"] = tools
        if response_format is not None:
            payload["format"] = response_format

        start = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        calls: list[ToolCall] = []
        final: dict[str, Any] = {}
        for chunk in self._post_lines("/api/chat", payload):
            if "error" in chunk:
                raise RuntimeError(f"ollama error: {chunk['error']}")
            msg = chunk.get("message") or {}
            content = msg.get("content") or ""
            if ttft is None and (content or msg.get("thinking") or msg.get("tool_calls")):
                ttft = time.perf_counter() - start
            parts.append(content)
            for tc in msg.get("tool_calls") or []:
                calls.append(_parse_tool_call(tc))
            if chunk.get("done"):
                final = chunk
        total = time.perf_counter() - start

        return GenerateResult(
            text="".join(parts),
            tool_calls=calls,
            prompt_tokens=int(final.get("prompt_eval_count", 0)),
            completion_tokens=int(final.get("eval_count", 0)),
            ttft_s=ttft,
            total_s=total,
            load_s=final.get("load_duration", 0) / _NS,
            prompt_eval_s=final.get("prompt_eval_duration", 0) / _NS,
            eval_s=final.get("eval_duration", 0) / _NS,
            model=self.config.ollama_model,
        )

    def load(self) -> float:
        """Load the model into memory. Returns load time in seconds."""
        start = time.perf_counter()
        for _ in self._post_lines(
            "/api/generate",
            {
                "model": self.config.ollama_model,
                "keep_alive": self.config.keep_alive,
                # Same num_ctx as every chat request, so the next request never reloads.
                "options": {"num_ctx": self.config.context_length},
            },
        ):
            pass
        return time.perf_counter() - start

    def unload(self) -> None:
        for _ in self._post_lines(
            "/api/generate", {"model": self.config.ollama_model, "keep_alive": 0}
        ):
            pass

    def runtime_info(self) -> dict[str, Any]:
        version = self._get_json("/api/version").get("version", "?")
        loaded = self._get_json("/api/ps").get("models", [])
        mine = [m for m in loaded if m.get("name") == self.config.ollama_model]
        return {"runtime": "ollama", "version": version, "loaded": mine}


def _to_ollama(message: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        out["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in message.tool_calls
        ]
    if message.tool_name:
        out["tool_name"] = message.tool_name
    return out


def _parse_tool_call(tc: dict[str, Any]) -> ToolCall:
    fn = tc.get("function") or {}
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        args = json.loads(args)  # raises if the model produced invalid JSON
    if not isinstance(args, dict):
        raise ValueError("tool call arguments must be a JSON object")
    return ToolCall(name=str(fn.get("name", "")), arguments=args)
