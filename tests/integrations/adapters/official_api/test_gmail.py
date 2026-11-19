"""Step 15: Gmail login, REST client and tool rules, with a fake Google (httpx MockTransport and
an in-memory credential store). Nothing here touches the network, keyring or a real account."""

from __future__ import annotations

import base64
import json
import re
import threading
from email import message_from_bytes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from integrations.adapters.official_api import gmail_api, gmail_tools
from integrations.adapters.official_api.gmail_api import GmailApi, GmailError, build_raw
from integrations.adapters.official_api.gmail_auth import (
    KEY_CLIENT,
    KEY_REFRESH,
    SCOPES,
    GmailLoginNeeded,
    GmailSetupError,
    TokenProvider,
    import_client,
    login,
)
from integrations.adapters.official_api.gmail_tools import recipient_problem


class MemoryStore:
    def __init__(self, **items: str) -> None:
        self.items = dict(items)

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def set(self, key: str, value: str) -> None:
        self.items[key] = value

    def delete(self, key: str) -> None:
        self.items.pop(key, None)


CLIENT_JSON = {
    "installed": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-test-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def ready_store() -> MemoryStore:
    return MemoryStore(
        **{
            KEY_CLIENT: json.dumps({"client_id": "id", "client_secret": "secret"}),
            KEY_REFRESH: "refresh-1",
        }
    )


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ---------- scopes and the client file ----------


def test_scopes_are_the_least_that_work() -> None:
    assert set(SCOPES) == {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    }
    assert not any("mail.google.com" in s or "settings" in s or "contacts" in s for s in SCOPES)


def test_api_module_has_no_delete_label_settings_or_contacts_call() -> None:
    source = Path(gmail_api.__file__).read_text(encoding="utf-8")
    paths = set(re.findall(r'"(?:GET|POST)",\s*f?"(/[^"]*)"', source))
    assert paths == {"/profile", "/messages", "/messages/{item['id']}", "/messages/{message_id}",
                     "/drafts", "/messages/send"}  # fmt: skip
    for forbidden in ('"DELETE"', '"PUT"', '"PATCH"', "/trash", "/modify", "/settings", "people"):
        assert forbidden not in source


def test_import_client_keeps_only_id_and_secret_in_the_store(tmp_path: Path) -> None:
    file = tmp_path / "client.json"
    file.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    store = MemoryStore()
    assert import_client(file, store) == "123-abc.apps.googleusercontent.com"
    assert json.loads(store.items[KEY_CLIENT]) == {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-test-secret",
    }
    assert file.exists()  # left where the owner keeps it


@pytest.mark.parametrize(
    "content", ["not json", json.dumps({"web": {"client_id": "x"}}), json.dumps({"installed": {}})]
)
def test_import_client_rejects_other_files(tmp_path: Path, content: str) -> None:
    file = tmp_path / "client.json"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(GmailSetupError):
        import_client(file, MemoryStore())


# ---------- login ----------


def _token_transport(
    scope: str = " ".join(SCOPES),
    refresh: str | None = "refresh-new",
    seen: list[Any] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        form = parse_qs(request.content.decode())
        if seen is not None:
            seen.append(form)
        body: dict[str, Any] = {"access_token": "at", "expires_in": 3600, "scope": scope}
        if refresh:
            body["refresh_token"] = refresh
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _browser(*, state: str | None = None, error: str | None = None, seen: list[str]) -> Any:
    """Plays the owner: opens the consent URL and lets Google redirect to the loopback."""

    def open_browser(url: str) -> None:
        seen.append(url)
        query = parse_qs(urlsplit(url).query)
        redirect = query["redirect_uri"][0]
        params = {"state": state or query["state"][0]}
        params.update({"error": error} if error else {"code": "auth-code"})
        threading.Thread(target=lambda: httpx.get(redirect, params=params), daemon=True).start()

    return open_browser


def test_login_runs_pkce_on_loopback_and_stores_only_the_refresh_token() -> None:
    store, urls, forms = ready_store(), [], []
    del store.items[KEY_REFRESH]
    login(store, open_browser=_browser(seen=urls), transport=_token_transport(seen=forms))
    query = parse_qs(urlsplit(urls[0]).query)
    assert urlsplit(urls[0]).netloc == "accounts.google.com"
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert query["code_challenge_method"] == ["S256"] and query["scope"] == [" ".join(SCOPES)]
    assert forms[0]["code_verifier"][0] and forms[0]["code"] == ["auth-code"]
    assert store.items[KEY_REFRESH] == "refresh-new"


def test_login_refuses_a_wrong_state_and_saves_nothing() -> None:
    store = ready_store()
    del store.items[KEY_REFRESH]
    with pytest.raises(GmailSetupError, match="did not match"):
        login(store, open_browser=_browser(state="forged", seen=[]), transport=_token_transport())
    assert KEY_REFRESH not in store.items


def test_login_refuses_when_the_owner_says_no_or_scopes_differ() -> None:
    store = ready_store()
    del store.items[KEY_REFRESH]
    with pytest.raises(GmailSetupError, match="access_denied"):
        login(store, open_browser=_browser(error="access_denied", seen=[]))
    wider = " ".join([*SCOPES, "https://mail.google.com/"])
    with pytest.raises(GmailSetupError, match="more than"):
        login(store, open_browser=_browser(seen=[]), transport=_token_transport(scope=wider))
    with pytest.raises(GmailSetupError, match="not all granted"):
        login(store, open_browser=_browser(seen=[]), transport=_token_transport(scope=SCOPES[0]))
    assert KEY_REFRESH not in store.items


# ---------- access tokens and the expired login ----------


def test_token_is_refreshed_once_and_cached() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"at{len(calls)}", "expires_in": 3600})

    now = [0.0]
    tokens = TokenProvider(
        ready_store(), transport=httpx.MockTransport(handler), clock=lambda: now[0]
    )
    assert tokens.access_token() == "at1" and tokens.access_token() == "at1"
    now[0] = 4000.0
    assert tokens.access_token() == "at2" and len(calls) == 2


def test_expired_or_missing_login_says_log_in_again() -> None:
    expired = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(GmailLoginNeeded, match="please log in to Gmail again"):
        TokenProvider(ready_store(), transport=expired).access_token()
    empty = ready_store()
    del empty.items[KEY_REFRESH]
    with pytest.raises(GmailLoginNeeded, match="please log in to Gmail again"):
        TokenProvider(empty).access_token()


# ---------- the REST client ----------


def _api(handler: Any) -> GmailApi:
    token = httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "at"}))
    return GmailApi(
        TokenProvider(ready_store(), transport=token), transport=httpx.MockTransport(handler)
    )


def _message(mid: str = "18c0ffee00000001") -> dict[str, Any]:
    return {
        "id": mid,
        "snippet": "Hi there",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Anna <anna@example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Lunch"},
                {"name": "Date", "value": "Wed, 18 Nov 2026 10:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"data": b64("See you at noon.")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "menu.pdf",
                    "body": {"attachmentId": "x"},
                },
            ],
        },
    }


def test_search_and_read_use_only_gmail_urls_with_the_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "18c0ffee00000001"}]})
        return httpx.Response(200, json=_message())

    api = _api(handler)
    rows = api.search("from:anna", 3)
    content = api.read("18c0ffee00000001")
    assert rows[0].sender == "Anna <anna@example.com>" and rows[0].subject == "Lunch"
    assert content.body == "See you at noon." and content.attachments == ["menu.pdf"]
    assert {r.url.host for r in seen} == {"gmail.googleapis.com"}
    assert all(r.headers["authorization"] == "Bearer at" for r in seen)
    assert all(r.method == "GET" for r in seen)


def test_html_only_mail_becomes_visible_text_without_scripts() -> None:
    html = (
        "<html><head><style>p{}</style></head>"
        "<body><p>Hello</p><script>evil()</script></body></html>"
    )
    raw = _message()
    raw["payload"]["parts"] = [{"mimeType": "text/html", "body": {"data": b64(html)}}]
    assert gmail_api.parse_message(raw).body == "Hello"


def test_a_long_body_is_cut_and_marked() -> None:
    raw = _message()
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "body": {"data": b64("x" * 9000)}}]
    parsed = gmail_api.parse_message(raw)
    assert len(parsed.body) == gmail_api.MAX_BODY_CHARS and parsed.truncated


def test_a_401_refreshes_once_then_asks_to_log_in_again() -> None:
    counts = {"gmail": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["gmail"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid"}})

    with pytest.raises(GmailLoginNeeded, match="please log in to Gmail again"):
        _api(handler).profile_address()
    assert counts["gmail"] == 2


def test_gmail_errors_are_short_and_readable() -> None:
    api = _api(lambda r: httpx.Response(429, json={}))
    with pytest.raises(GmailError, match="too many requests"):
        api.profile_address()


def test_send_builds_a_plain_message_and_refuses_header_injection() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.url.path.endswith("/messages/send")
        return httpx.Response(200, json={"id": "m1"})

    assert _api(handler).send(["bob@example.com"], "Hello", "Line 1\nLine 2") == "m1"
    mail = message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]))
    assert mail["To"] == "bob@example.com" and mail["Subject"] == "Hello"
    assert "Line 1\nLine 2" in mail.get_payload(decode=True).decode()
    with pytest.raises(ValueError):
        build_raw(["bob@example.com"], "Hi\nBcc: x@evil.com", "body")


# ---------- the recipient rule ----------


@pytest.mark.parametrize(
    ("recipients", "words", "own", "ok"),
    [
        (["bob@example.com"], "Send an email to bob@example.com that it is ready", None, True),
        (["Bob@Example.com"], "send bob@example.com a note.", None, True),
        (["x@evil.com"], "Read my latest email", "me@example.com", False),
        (["x@evil.com"], "send to x@evil.com.au", None, False),  # a longer address is not it
        (["x@evil.com"], "reply to ax@evil.com", None, False),
        (["me"], "Send myself a reminder", "me@example.com", True),
        (["me"], "Send a reminder to bob", "me@example.com", False),  # no "me" in the words
        (["me"], "Send myself a reminder", None, False),  # own address unknown
        (["me@example.com"], "Read my latest email", "me@example.com", True),
        (["bob@example.com", "x@evil.com"], "send to bob@example.com", None, False),
    ],
)
def test_recipient_rule(recipients: list[str], words: str, own: str | None, ok: bool) -> None:
    assert (recipient_problem(recipients, words, own) is None) is ok


@pytest.mark.parametrize(
    "to",
    [["not-an-address"], ["a@b.com\nBcc: x@evil.com"], ["a@b.com, c@d.com"], [], ["Bob <b@c.com>"]],
)
def test_compose_input_rejects_odd_recipients(to: list[str]) -> None:
    with pytest.raises(ValueError):
        gmail_tools.SendMessageInput(to=to, subject="s", body="b")


def test_compose_input_rejects_bad_subject_and_body() -> None:
    with pytest.raises(ValueError):
        gmail_tools.SendMessageInput(to=["a@b.com"], subject="a\nb", body="b")
    with pytest.raises(ValueError):
        gmail_tools.SendMessageInput(to=["a@b.com"], subject="s", body="x" * 5001)
    with pytest.raises(ValueError):
        gmail_tools.SendMessageInput(to=["a@b.com"], subject="s", body="bad\x00")


def test_the_gmail_manifests_ship_quarantined_with_the_right_tiers() -> None:
    specs = {s.name: s.manifest for s in gmail_tools.default_gmail_tool_specs()}
    assert {n: m.permission_tier.value for n, m in specs.items()} == {
        "search_messages": "T1",
        "read_message": "T1",
        "draft_message": "T2",
        "send_message": "T3",
    }
    assert specs["send_message"].approval_required
    assert specs["send_message"].side_effects == "external_action"
    assert specs["draft_message"].side_effects == "writes"
    assert all(m.status == gmail_tools.GMAIL_TOOLS_STATUS for m in specs.values())
    assert all(set(m.permissions.network) == {"gmail.googleapis.com", "oauth2.googleapis.com"}
               for m in specs.values())  # fmt: skip
