"""Redaction before anything is sent to a cloud model (owner decision 2026-11-26).

1. Protected paths (architecture rule 10): any path that is, or is inside, a protected path is
   replaced, and a tool result that names one is withheld whole (its text could be that
   path's contents). Tools already refuse protected paths; this is a second line.
2. Secret patterns: private keys, provider API keys, bearer/JWT tokens, `password=` style
   pairs and card numbers (Luhn-checked).

Pattern matching can miss things; the warning the owner accepts says so.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from model.serving.types import Message, ToolCall

REDACTED = "[redacted]"
WITHHELD = "[withheld: this tool result refers to a protected path]"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S
)
_SECRET_TOKENS = [
    re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic / OpenRouter
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),  # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),  # Google OAuth access token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
]
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{12,}=*")
_PAIR = re.compile(
    r"(?i)\b((?:api[_\- ]?key|secret|token|access[_\- ]?key|client[_\- ]?secret|"
    r"passw(?:or)?d|passwd|pwd|pin)\s*[\"']?\s*[:=]\s*)([\"']?)([^\s\"',;]{4,})"
)
_CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
# Windows (C:\..., C:/...) and home-relative (~/...) paths, up to whitespace or a quote.
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|~[\\/])[^\s\"'<>|*?]*")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return REDACTED
    return match.group(0)


def redact_secrets(text: str) -> str:
    text = _PRIVATE_KEY.sub(REDACTED, text)
    for pattern in _SECRET_TOKENS:
        text = pattern.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _CARD.sub(_card, text)


IsProtected = Callable[[Path], bool]


def default_is_protected() -> IsProtected:
    from security.permissions.path_check import PathPolicy

    policy = PathPolicy.from_config()
    return policy.is_protected


class Redactor:
    def __init__(self, is_protected: IsProtected | None = None) -> None:
        self._is_protected = is_protected

    def _protected(self, raw: str) -> bool:
        if self._is_protected is None:
            self._is_protected = default_is_protected()
        candidate = raw.rstrip(".,;:)]}")
        if candidate.startswith(("~/", "~\\")):
            candidate = str(Path.home() / candidate[2:])
        try:
            return self._is_protected(Path(candidate))
        except (OSError, ValueError):
            return False

    def mentions_protected(self, text: str) -> bool:
        return any(self._protected(m.group(0)) for m in _PATH.finditer(text))

    def text(self, text: str) -> str:
        text = _PATH.sub(lambda m: REDACTED if self._protected(m.group(0)) else m.group(0), text)
        return redact_secrets(text)

    def _value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self._value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._value(v) for k, v in value.items()}
        return value

    def messages(self, messages: list[Message]) -> list[Message]:
        out: list[Message] = []
        for m in messages:
            if m.role == "tool" and self.mentions_protected(m.content):
                content = WITHHELD
            else:
                content = self.text(m.content)
            calls = None
            if m.tool_calls:
                calls = [
                    ToolCall(name=c.name, arguments=self._value(c.arguments))  # type: ignore[arg-type]
                    for c in m.tool_calls
                ]
            out.append(m.model_copy(update={"content": content, "tool_calls": calls}))
        return out
