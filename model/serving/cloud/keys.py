"""Cloud API keys in the OS credential store only (architecture rule 11).

Service `hcwma`, user `cloud_api_key:<provider>`. A key is never written to a file, a log,
the audit log or an HTTP response; callers get it only to put it in a request header.
"""

from __future__ import annotations

import os
from typing import Protocol

SERVICE_NAME = "hcwma"


class KeyStoreError(RuntimeError):
    """The credential store could not be used. Safe to show (never contains the key)."""


class KeyStore(Protocol):
    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, key: str) -> None: ...

    def delete(self, provider: str) -> None: ...


def _user(provider: str) -> str:
    return f"cloud_api_key:{provider}"


def _keyring_off() -> bool:
    # Tests set this: no test may read or change the owner's real credential store.
    return os.environ.get("HCWMA_DISABLE_KEYRING") == "1"


class KeyringKeyStore:
    def get(self, provider: str) -> str | None:
        if _keyring_off():
            return None
        import keyring

        try:
            return keyring.get_password(SERVICE_NAME, _user(provider))
        except Exception as exc:  # noqa: BLE001 - a locked store is "no key"
            raise KeyStoreError("the credential store could not be read") from exc

    def set(self, provider: str, key: str) -> None:
        if _keyring_off():
            raise KeyStoreError("the credential store is switched off (HCWMA_DISABLE_KEYRING)")
        key = key.strip()
        if not key or len(key) > 1000 or any(c.isspace() for c in key):
            raise KeyStoreError("that does not look like an API key")
        import keyring

        keyring.set_password(SERVICE_NAME, _user(provider), key)

    def delete(self, provider: str) -> None:
        if _keyring_off():
            return
        import keyring

        try:
            keyring.delete_password(SERVICE_NAME, _user(provider))
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryKeyStore:
    """For tests."""

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        self.keys = dict(keys or {})

    def get(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def set(self, provider: str, key: str) -> None:
        self.keys[provider] = key

    def delete(self, provider: str) -> None:
        self.keys.pop(provider, None)
