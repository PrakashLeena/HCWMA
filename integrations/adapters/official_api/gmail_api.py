"""Gmail REST client (build step 15): the official Gmail API over `httpx`.

Only these calls exist: list/get messages, get profile, create draft, send message. There is no
delete, no label or filter change, no settings and no contacts call, and the scopes the login
asks for could not do those anyway. Every URL is built from one fixed base, so a message
cannot steer a request anywhere else. Message text is returned as plain data: HTML is turned
into visible text and nothing is ever opened or run.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

import httpx

from integrations.adapters.official_api.gmail_auth import (
    HTTP_TIMEOUT_S,
    GmailLoginNeeded,
    TokenProvider,
)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_BODY_CHARS = 3000  # what one read may carry back to the local model
_HEADERS = ("From", "To", "Subject", "Date")


class GmailError(RuntimeError):
    """Gmail answered with an error. The message is safe to show to the agent and the owner."""


@dataclass(frozen=True)
class MessageSummary:
    id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str


@dataclass(frozen=True)
class MessageContent:
    summary: MessageSummary
    body: str
    truncated: bool
    attachments: list[str] = field(default_factory=list)


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head", "template"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    return _tidy("".join(parser.parts))


def _tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts") or []:
        parts.extend(_walk(child))
    return parts


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers") or []:
        if str(h.get("name", "")).lower() == name.lower():
            return " ".join(str(h.get("value", "")).split())[:300]
    return ""


def _summary(raw: dict[str, Any]) -> MessageSummary:
    payload = raw.get("payload") or {}
    return MessageSummary(
        id=str(raw.get("id", "")),
        sender=_header(payload, "From"),
        to=_header(payload, "To"),
        subject=_header(payload, "Subject"),
        date=_header(payload, "Date"),
        snippet=" ".join(str(raw.get("snippet", "")).split())[:200],
    )


def parse_message(raw: dict[str, Any]) -> MessageContent:
    """A Gmail `format=full` message as plain text. Prefers text/plain; falls back to HTML."""
    payload = raw.get("payload") or {}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    for part in _walk(payload):
        mime = str(part.get("mimeType", ""))
        if part.get("filename"):
            attachments.append(str(part["filename"])[:100])
            continue
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            html.append(_decode(data))
    text = _tidy("\n".join(plain)) if plain else html_to_text("\n".join(html))
    return MessageContent(
        summary=_summary(raw),
        body=text[:MAX_BODY_CHARS],
        truncated=len(text) > MAX_BODY_CHARS,
        attachments=attachments[:10],
    )


def build_raw(to: list[str], subject: str, body: str) -> str:
    """The RFC 822 message, base64url, as Gmail expects. Header injection is refused by
    `EmailMessage` itself (a newline in a header raises)."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailApi:
    def __init__(
        self,
        tokens: TokenProvider | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._tokens = tokens or TokenProvider()
        self._transport = transport

    # ---------- plumbing ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in (1, 2):
            token = self._tokens.access_token()
            try:
                with httpx.Client(
                    timeout=HTTP_TIMEOUT_S, trust_env=False, transport=self._transport
                ) as http:
                    response = http.request(
                        method,
                        API + path,
                        params=params,
                        json=json,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.TimeoutException as exc:
                raise GmailError("Gmail did not answer in time") from exc
            except httpx.HTTPError as exc:
                raise GmailError(f"Gmail could not be reached ({type(exc).__name__})") from exc
            if response.status_code == 401 and attempt == 1:
                self._tokens.forget()  # the token may just have expired: refresh once
                continue
            if response.status_code == 401:
                raise GmailLoginNeeded()
            if response.status_code >= 400:
                raise GmailError(_error_text(response))
            return response.json() if response.content else {}
        raise GmailLoginNeeded()  # pragma: no cover - the loop always returns or raises

    # ---------- calls ----------

    def profile_address(self) -> str:
        return str(self._request("GET", "/profile").get("emailAddress", ""))

    def search(self, query: str, max_results: int) -> list[MessageSummary]:
        listing = self._request("GET", "/messages", params={"q": query, "maxResults": max_results})
        found: list[MessageSummary] = []
        for item in listing.get("messages") or []:
            raw = self._request(
                "GET",
                f"/messages/{item['id']}",
                params={"format": "metadata", "metadataHeaders": list(_HEADERS)},
            )
            found.append(_summary(raw))
        return found

    def read(self, message_id: str) -> MessageContent:
        return parse_message(
            self._request("GET", f"/messages/{message_id}", params={"format": "full"})
        )

    def create_draft(self, to: list[str], subject: str, body: str) -> str:
        made = self._request(
            "POST", "/drafts", json={"message": {"raw": build_raw(to, subject, body)}}
        )
        return str(made.get("id", ""))

    def send(self, to: list[str], subject: str, body: str) -> str:
        sent = self._request("POST", "/messages/send", json={"raw": build_raw(to, subject, body)})
        return str(sent.get("id", ""))


def _error_text(response: httpx.Response) -> str:
    reason = ""
    try:
        reason = str(response.json().get("error", {}).get("message", ""))
    except ValueError:
        pass
    reason = " ".join(reason.split())[:150]
    if response.status_code == 403:
        return f"Gmail refused this request ({reason or 'permission'})"
    if response.status_code == 429:
        return "Gmail says too many requests; try again in a minute"
    return f"Gmail returned an error (HTTP {response.status_code}){': ' + reason if reason else ''}"
