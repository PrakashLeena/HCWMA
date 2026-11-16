"""Per-install API token (master document 10.3).

The token stops other local programs and web pages from driving HCWMA silently. It is
created on first use and kept in the OS credential store through `keyring` — never in a
file, a log or Git (architecture rule 11).

Show it (to paste into the chat page):   python -m backend.auth.token
Replace it with a new one:                python -m backend.auth.token --rotate
"""

from __future__ import annotations

import argparse
import hmac
import secrets
from typing import Protocol

import keyring

SERVICE_NAME = "hcwma"
TOKEN_KEY = "local-api-token"  # noqa: S105 - the name of the entry, not the secret
_TOKEN_BYTES = 32


class TokenStore(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


def get_or_create_token(store: TokenStore = keyring) -> str:
    token = store.get_password(SERVICE_NAME, TOKEN_KEY)
    if token:
        return token
    return rotate_token(store)


def rotate_token(store: TokenStore = keyring) -> str:
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    store.set_password(SERVICE_NAME, TOKEN_KEY, token)
    return token


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison. An empty expected token never matches."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Show or rotate the HCWMA local API token.")
    parser.add_argument("--rotate", action="store_true", help="replace the token with a new one")
    args = parser.parse_args()
    token = rotate_token() if args.rotate else get_or_create_token()
    # Printed only because the owner asked for it in their own terminal.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
