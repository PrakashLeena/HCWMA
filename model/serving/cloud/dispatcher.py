"""Picks the model for each call: the local Qwen or the owner's cloud model.

`ModelDispatcher.generate` is what the shared model queue calls (architecture rule 3: every
call, local or cloud, still goes through `agents/runtime/model_queue.py`). It reads the
owner's settings on every call, so a change on the cloud page applies to the next call.

- Local only (default), or no model / warning not accepted -> local.
- Cloud call: redacted first; budget checked first. Network error, timeout, rate limit,
  refused key, missing key or budget reached -> the same call runs locally and the result
  carries `fallback_note` for the chat.
- Each cloud call (and each fallback) is written to the audit log with provider, model,
  agent, token counts, cost and outcome. Never the prompt or reply text.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from model.serving.cloud.budget import UsageLedger, cost_of
from model.serving.cloud.keys import KeyringKeyStore, KeyStore, KeyStoreError
from model.serving.cloud.providers import CloudError, make_cloud_backend
from model.serving.cloud.redact import Redactor
from model.serving.cloud.settings import CloudSettings, CloudSettingsStore
from model.serving.types import GenerateResult, Message

LocalGenerate = Callable[..., GenerateResult]
BackendFactory = Callable[[CloudSettings, str], Any]

_REASONS = {
    "network": "could not reach the cloud provider",
    "timeout": "the cloud provider did not answer in time",
    "rate_limit": "the cloud provider's rate limit was reached",
    "auth": "the cloud provider refused the API key (check it on the Model page)",
    "credits": "the cloud account has no credits or quota left",
    "server": "the cloud provider had an error",
    "bad_request": "the cloud provider refused the request",
    "invalid": "the cloud model's reply could not be read",
    "budget": "this month's cloud budget is used up",
    "empty": "the cloud model gave an empty answer",
    "empty_length": "the cloud model spent its whole answer budget thinking and gave no answer "
    '(turn on "Thinking off" for this provider on the Model page)',
    "no_key": "no cloud API key is saved",
}


class CloudUnavailable(RuntimeError):
    """A cloud call failed and falling back to the local model is off. The message is safe
    to show in the chat: the reason only, never the key, prompt or provider's reply."""

    def __init__(self, kind: str, model: str) -> None:
        self.kind = kind
        self.model = model
        super().__init__(
            f"The cloud model ({model}) did not answer: {_REASONS.get(kind, 'the call failed')}. "
            'Nothing was run on the local model; turn on "Fall back to local if cloud fails" '
            "on the Model page to allow that."
        )


class LocalUnavailable(RuntimeError):
    """The local model could not be started (Ollama down). Safe to show."""


def fallback_note(kind: str) -> str:
    return f"Answered by the local model: {_REASONS.get(kind, 'the cloud call failed')}."


def _default_factory(settings: CloudSettings, key: str) -> Any:
    return make_cloud_backend(
        settings.provider,
        base_url=settings.base_url,
        model=settings.model,
        api_key=key,
        timeout_s=settings.request_timeout_s,
        thinking_off=settings.thinking_off,
    )


class ModelDispatcher:
    def __init__(
        self,
        local_generate: LocalGenerate | None = None,
        *,
        settings: CloudSettingsStore | None = None,
        keys: KeyStore | None = None,
        ledger: UsageLedger | None = None,
        redactor: Redactor | None = None,
        backend_factory: BackendFactory | None = None,
        audit: Any | None = None,
    ) -> None:
        self._local = local_generate
        self.settings = settings or CloudSettingsStore()
        self.keys = keys or KeyringKeyStore()
        self.ledger = ledger or UsageLedger()
        self._redactor = redactor or Redactor()
        self._factory = backend_factory or _default_factory
        self._audit = audit
        self.transport: Any = None  # httpx transport for the model list (tests)
        # Called before a local call in Hybrid/Cloud mode: starts Ollama only when a local
        # call really happens (the backend sets it to the Ollama launcher). Raises
        # LocalUnavailable when it cannot.
        self.local_ready: Callable[[], None] | None = None
        # Last cloud outcome this run ("ok", "fallback:rate_limit", ...) for the Health page.
        self.last_outcome: str | None = None
        self.last_at: float | None = None

    # -- helpers ---------------------------------------------------------------------------

    def _local_generate(self, *args: Any, **kwargs: Any) -> GenerateResult:
        if self._local is None:
            from model.serving import get_backend

            self._local = get_backend().generate
        return self._local(*args, **kwargs)

    def _audit_logger(self) -> Any:
        if self._audit is None:
            from security.audit.audit_logger import get_audit_logger

            return get_audit_logger()
        return self._audit

    def target_for(self, agent: str | None) -> str:
        return self.settings.load().target_for(agent)

    # -- the call ----------------------------------------------------------------------------

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        *,
        agent: str | None = None,
        task_id: str | None = None,
    ) -> GenerateResult:
        options = {
            "tools": tools,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "temperature": temperature,
            "seed": seed,
        }
        settings = self.settings.load()
        if settings.target_for(agent) == "local":
            if settings.mode != "local" and self.local_ready is not None:
                self.local_ready()
            return self._local_generate(messages, **options)

        def fall_back(kind: str, started: float) -> GenerateResult:
            if not settings.fallback_to_local:
                self._record(settings, agent, task_id, started, outcome=f"failed:{kind}")
                raise CloudUnavailable(kind, settings.model)
            self.ledger.add(fallback=True)
            self._record(settings, agent, task_id, started, outcome=f"fallback:{kind}")
            if self.local_ready is not None:
                self.local_ready()
            result = self._local_generate(messages, **options)
            return result.model_copy(update={"fallback_note": fallback_note(kind)})

        start = time.perf_counter()
        month = self.ledger.this_month()
        if settings.budget_blocks(month.cost_usd):
            return fall_back("budget", start)
        try:
            key = self.keys.get(settings.key_id)
        except KeyStoreError:
            key = None
        if not key:
            return fall_back("no_key", start)
        try:
            backend = self._factory(settings, key)
            result = backend.generate(self._redactor.messages(messages), **options)
        except CloudError as exc:
            return fall_back(exc.kind, start)
        except ValueError:  # a tool call that does not validate, a bad setting
            return fall_back("invalid", start)
        cost = cost_of(
            result.prompt_tokens,
            result.completion_tokens,
            settings.price_input_per_mtok,
            settings.price_output_per_mtok,
        )
        self.ledger.add(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=cost,
        )
        self._record(settings, agent, task_id, start, outcome="ok", result=result, cost=cost)
        return result.model_copy(update={"cost_usd": cost})

    def _record(
        self,
        settings: CloudSettings,
        agent: str | None,
        task_id: str | None,
        start: float,
        *,
        outcome: str,
        result: GenerateResult | None = None,
        cost: float = 0.0,
    ) -> None:
        self.last_outcome, self.last_at = outcome, time.time()
        try:
            self._audit_logger().cloud_call(
                task_id=task_id or "-",
                agent=agent or "unknown",
                provider=settings.provider,
                model=settings.model,
                prompt_tokens=result.prompt_tokens if result else 0,
                completion_tokens=result.completion_tokens if result else 0,
                cost_usd=cost,
                outcome=outcome,
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        except OSError:
            pass  # the audit file is not writable: the call itself still counts

    # -- "Test connection" -------------------------------------------------------------------

    def test_connection(self, settings: CloudSettings | None = None) -> dict[str, Any]:
        """One tiny request to the configured provider. Returns ok, seconds and a safe reason."""
        settings = settings or self.settings.load()
        if not settings.model:
            return {"ok": False, "detail": "set a model name first"}
        try:
            key = self.keys.get(settings.key_id)
        except KeyStoreError as exc:
            return {"ok": False, "detail": str(exc)}
        if not key:
            return {"ok": False, "detail": "no API key saved for this provider"}
        start = time.perf_counter()
        try:
            backend = self._factory(settings, key)
            result = backend.generate(
                [Message(role="user", content="Reply with the single word OK.")], max_tokens=16
            )
        except CloudError as exc:
            return {"ok": False, "detail": str(exc), "kind": exc.kind}
        except ValueError as exc:
            return {"ok": False, "detail": str(exc)}
        seconds = round(time.perf_counter() - start, 2)
        cost = cost_of(
            result.prompt_tokens,
            result.completion_tokens,
            settings.price_input_per_mtok,
            settings.price_output_per_mtok,
        )
        self.ledger.add(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=cost,
        )
        self._record(
            settings, "test_connection", None, start, outcome="ok", result=result, cost=cost
        )
        return {"ok": True, "seconds": seconds, "model": settings.model, "reply": result.text[:40]}


def health_value(dispatcher: ModelDispatcher) -> tuple[bool | None, str]:
    """The Health page's "Cloud model" line. Reads settings and counters only: no network."""
    settings = dispatcher.settings.load()
    if settings.mode == "local":
        return True, "off (Local only)"
    mode = {"hybrid": "Hybrid", "cloud": "Cloud"}[settings.mode]
    try:
        key = bool(dispatcher.keys.get(settings.key_id))
    except KeyStoreError:
        key = False
    month = dispatcher.ledger.this_month()
    parts = [
        f"{mode}: {settings.key_id} {settings.model or '(no model set)'}",
        "key saved" if key else "NO API KEY",
        f"${month.cost_usd:.2f} of "
        + (
            f"${settings.monthly_budget_usd:.2f}"
            if settings.monthly_budget_usd > 0
            else ("$0.00" if settings.budget_blocks(0) else "unlimited (free, unpriced)")
        )
        + " this month, "
        f"{month.calls} calls, {month.fallbacks} fallbacks",
    ]
    ok: bool | None = key and not settings.budget_blocks(month.cost_usd)
    if dispatcher.last_outcome is not None:
        parts.append(f"last call: {dispatcher.last_outcome}")
        if dispatcher.last_outcome.startswith("fallback"):
            ok = False if ok else ok
    return ok, "; ".join(parts)


def list_models(dispatcher: ModelDispatcher, profile_id: str) -> list[str]:
    """The provider's model ids for a saved profile (one GET; no tokens are used)."""
    from model.serving.cloud.providers import fetch_models

    settings = dispatcher.settings.load()
    profile = settings.profiles.get(profile_id)
    if profile is None:
        raise CloudError("bad_request", "unknown profile")
    try:
        key = dispatcher.keys.get(profile_id)
    except KeyStoreError as exc:
        raise CloudError("auth", str(exc)) from None
    if not key:
        raise CloudError("auth", "no API key saved for this profile")
    return fetch_models(
        profile.provider, base_url=profile.base_url, api_key=key, transport=dispatcher.transport
    )
