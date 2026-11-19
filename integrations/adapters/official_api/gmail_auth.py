"""Gmail sign-in (build step 15; master document 6.3 route 2, 9.2, architecture rule 11).

Official Google OAuth 2.0 "installed application" flow with PKCE and a loopback redirect, done
with `httpx` only (no Google client library, so no new dependency):

- The OAuth client (client id + secret) is imported once from the JSON file Google Cloud gives
  the owner and kept in the OS credential store (`keyring`). The file is never copied into the
  repository and never read again.
- The login opens the owner's own browser at accounts.google.com; HCWMA listens on
  `127.0.0.1` only, for this one redirect. HCWMA never sees the owner's Google password.
- Only the refresh token is stored (`keyring`). Access tokens live in memory for about an hour.
- Scopes are the least that do the job: read mail, and compose (drafts + send). Nothing that
  deletes, changes settings or reaches contacts.
- When Google refuses the refresh token (expired, revoked, or a "testing" app after 7 days)
  every tool says "please log in to Gmail again" instead of failing with a traceback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SERVICE = "HCWMA"
KEY_CLIENT = "gmail_oauth_client"
KEY_REFRESH = "gmail_refresh_token"
KEY_ACCOUNT = "gmail_account_address"

SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",  # search and read
    "https://www.googleapis.com/auth/gmail.compose",  # drafts and send; no delete, no settings
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL; fixed on purpose
HTTP_TIMEOUT_S = 20.0
LOGIN_TIMEOUT_S = 180.0
RELOGIN_MESSAGE = "please log in to Gmail again"


class GmailLoginNeeded(RuntimeError):
    """The saved Gmail login is missing, expired or revoked. The message is for the owner."""

    def __init__(self, message: str = RELOGIN_MESSAGE) -> None:
        super().__init__(message)


class GmailSetupError(RuntimeError):
    """The OAuth client file or the login could not be used."""


class CredentialStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


def _keyring_off() -> bool:
    """Tests set this so no test can ever read or change the owner's real credentials, in this
    process or in the MCP server child process (which inherits the environment)."""
    return os.environ.get("HCWMA_DISABLE_KEYRING") == "1"


class KeyringStore:
    """The OS credential store (Windows Credential Manager), service name `HCWMA`."""

    def get(self, key: str) -> str | None:
        if _keyring_off():
            return None
        import keyring

        try:
            return keyring.get_password(SERVICE, key)
        except Exception as exc:  # noqa: BLE001 - a locked or missing store is "not logged in"
            raise GmailLoginNeeded("the credential store could not be read") from exc

    def set(self, key: str, value: str) -> None:
        if _keyring_off():
            raise GmailSetupError("the credential store is switched off (HCWMA_DISABLE_KEYRING)")
        import keyring

        keyring.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        if _keyring_off():
            return
        import keyring

        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass


# ---------- one-time import of the OAuth client file ----------


def import_client(path: Path, store: CredentialStore) -> str:
    """Move the client id and secret from Google's JSON file into the credential store.
    Returns the client id (not secret). The file itself is left where the owner keeps it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GmailSetupError(f"cannot read the OAuth client file: {type(exc).__name__}") from exc
    section = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise GmailSetupError(
            "this is not a desktop-app OAuth client file (it has no 'installed' section)"
        )
    client_id, client_secret = section.get("client_id"), section.get("client_secret")
    if not (isinstance(client_id, str) and client_id and isinstance(client_secret, str)):
        raise GmailSetupError("the OAuth client file has no client_id / client_secret")
    store.set(KEY_CLIENT, json.dumps({"client_id": client_id, "client_secret": client_secret}))
    return client_id


def _client(store: CredentialStore) -> tuple[str, str]:
    raw = store.get(KEY_CLIENT)
    if not raw:
        raise GmailSetupError(
            "Gmail is not set up yet: import the OAuth client file with "
            "`python -m integrations.adapters.official_api.gmail_login import-client <file>`"
        )
    data = json.loads(raw)
    return data["client_id"], data["client_secret"]


# ---------- login (browser, loopback, PKCE) ----------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _Redirect(BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = parse_qs(urlsplit(self.path).query)
        type(self).result = {k: v[0] for k, v in query.items()}
        type(self).done.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"HCWMA: Gmail login received. You can close this tab.")

    def log_message(self, *_args: object) -> None:  # keep the code and state out of any log
        pass


def login(
    store: CredentialStore,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout_s: float = LOGIN_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Run the consent flow and keep the refresh token. Raises `GmailSetupError` on failure."""
    client_id, client_secret = _client(store)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)

    handler = type("Redirect", (_Redirect,), {"result": {}, "done": threading.Event()})
    server = HTTPServer(("127.0.0.1", 0), handler)  # loopback only (architecture rule 8)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": " ".join(SCOPES),
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "access_type": "offline",
                    "prompt": "consent",
                }
            )
        )
        open_browser(url)
        if not handler.done.wait(timeout_s):
            raise GmailSetupError("the login was not finished in time")
        got = handler.result
    finally:
        server.shutdown()
        server.server_close()

    if got.get("state") != state:
        raise GmailSetupError("the login answer did not match this request; nothing was saved")
    if "code" not in got:
        raise GmailSetupError(f"Google did not give access ({got.get('error', 'no code')})")
    with httpx.Client(timeout=HTTP_TIMEOUT_S, trust_env=False, transport=transport) as http:
        response = http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": got["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
            },
        )
    body = response.json() if response.content else {}
    refresh = body.get("refresh_token")
    if response.status_code != 200 or not refresh:
        raise GmailSetupError("Google did not return a login that can be kept")
    granted = set(str(body.get("scope", "")).split())
    if not set(SCOPES) <= granted:
        raise GmailSetupError("the Gmail permissions were not all granted; nothing was saved")
    if granted - set(SCOPES) - {"openid"}:
        raise GmailSetupError("Google granted more than HCWMA asked for; nothing was saved")
    store.set(KEY_REFRESH, refresh)


def logout(store: CredentialStore) -> None:
    store.delete(KEY_REFRESH)
    store.delete(KEY_ACCOUNT)


def remember_account(store: CredentialStore, address: str) -> None:
    store.set(KEY_ACCOUNT, address)


def account_address(store: CredentialStore) -> str | None:
    return store.get(KEY_ACCOUNT)


# ---------- access tokens ----------


class TokenProvider:
    """Hands out a short-lived access token, refreshing it when needed. Never logs a token."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store: CredentialStore = store or KeyringStore()
        self._transport = transport
        self._clock = clock
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def forget(self) -> None:
        with self._lock:
            self._token, self._expires = "", 0.0

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires - 60:
                return self._token
            refresh = self._store.get(KEY_REFRESH)
            if not refresh:
                raise GmailLoginNeeded()
            try:
                client_id, client_secret = _client(self._store)
            except GmailSetupError as exc:
                raise GmailLoginNeeded(str(exc)) from exc
            try:
                with httpx.Client(
                    timeout=HTTP_TIMEOUT_S, trust_env=False, transport=self._transport
                ) as http:
                    response = http.post(
                        TOKEN_URL,
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "refresh_token": refresh,
                            "grant_type": "refresh_token",
                        },
                    )
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"Google could not be reached to refresh the login ({type(exc).__name__})"
                ) from exc
            if response.status_code in (400, 401):  # invalid_grant: expired or revoked
                self._token, self._expires = "", 0.0
                raise GmailLoginNeeded()
            if response.status_code != 200:
                raise RuntimeError(
                    f"Google refused the login refresh (HTTP {response.status_code})"
                )
            body = response.json()
            self._token = str(body["access_token"])
            self._expires = self._clock() + float(body.get("expires_in", 3000))
            return self._token
