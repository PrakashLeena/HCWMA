"""Cloud API keys in the OS credential store only (architecture rule 11).

One credential per profile, with its own target name: service `HCWMA-cloud-<profile>`,
user `api_key`. (2026-11-26 bug: keys first shared the service `hcwma`, and Windows target
names ignore case, so they collided with Gmail's `HCWMA` entry; keyring moves entries
between such targets and a later Gmail write displaced the key.) Every save is read back
and reported as saved only when the same key comes back.

A key is never written to a file, a log, the audit log or an HTTP response; callers get it
only to put it in a request header. Pages see at most its last 4 characters.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

SERVICE_PREFIX = "HCWMA-cloud-"
USER = "api_key"
# Before 2026-11-26 (read only, so a key saved then still works if it survived).
LEGACY_SERVICE = "hcwma"

_BEARER = re.compile(r"^\s*bearer\s+", re.IGNORECASE)
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class KeyStoreError(RuntimeError):
    """The credential store could not be used. Safe to show (never contains the key)."""


def normalize_key(raw: str) -> str:
    """Any provider's format (nvapi-, sk-, gsk_, AIza...); a pasted "Bearer " is dropped."""
    key = _BEARER.sub("", raw or "").strip().strip("\"'")
    if not 8 <= len(key) <= 1000 or any(c.isspace() for c in key):
        raise KeyStoreError("that does not look like an API key")
    return key


def key_hint(key: str | None) -> str | None:
    return f"…{key[-4:]}" if key else None


class KeyStore(Protocol):
    def get(self, key_id: str) -> str | None: ...

    def set(self, key_id: str, key: str) -> str: ...

    def delete(self, key_id: str) -> None: ...


def _keyring_off() -> bool:
    # Tests set this: no test may read or change the owner's real credential store.
    return os.environ.get("HCWMA_DISABLE_KEYRING") == "1"


def _check_id(key_id: str) -> str:
    if not _PROFILE.match(key_id):
        raise KeyStoreError("unknown profile name")
    return key_id


class KeyringKeyStore:
    def get(self, key_id: str) -> str | None:
        if _keyring_off():
            return None
        import keyring

        _check_id(key_id)
        try:
            value = keyring.get_password(SERVICE_PREFIX + key_id, USER)
            if not value:
                value = keyring.get_password(LEGACY_SERVICE, f"cloud_api_key:{key_id}")
        except Exception as exc:  # noqa: BLE001 - a locked store is "no key"
            raise KeyStoreError("the credential store could not be read") from exc
        return value or None

    def set(self, key_id: str, key: str) -> str:
        """Save, read back, and return the key only if the store gives the same one back."""
        if _keyring_off():
            raise KeyStoreError("the credential store is switched off (HCWMA_DISABLE_KEYRING)")
        import keyring

        key = normalize_key(key)
        _check_id(key_id)
        try:
            keyring.set_password(SERVICE_PREFIX + key_id, USER, key)
            back = keyring.get_password(SERVICE_PREFIX + key_id, USER)
        except Exception as exc:  # noqa: BLE001 - never echo the store's message (may hold data)
            raise KeyStoreError(
                f"the credential store refused the key ({type(exc).__name__})"
            ) from None
        if back != key:
            raise KeyStoreError("the key was written but could not be read back; not saved")
        return key

    def delete(self, key_id: str) -> None:
        if _keyring_off():
            return
        import keyring

        _check_id(key_id)
        for service, user in (
            (SERVICE_PREFIX + key_id, USER),
            (LEGACY_SERVICE, f"cloud_api_key:{key_id}"),
        ):
            try:
                keyring.delete_password(service, user)
            except keyring.errors.PasswordDeleteError:
                pass


class MemoryKeyStore:
    """For tests. `lose_writes` makes a store that says nothing and keeps nothing."""

    def __init__(self, keys: dict[str, str] | None = None, lose_writes: bool = False) -> None:
        self.keys = dict(keys or {})
        self.lose_writes = lose_writes

    def get(self, key_id: str) -> str | None:
        return self.keys.get(key_id)

    def set(self, key_id: str, key: str) -> str:
        key = normalize_key(key)
        if not self.lose_writes:
            self.keys[key_id] = key
        if self.get(key_id) != key:
            raise KeyStoreError("the key was written but could not be read back; not saved")
        return key

    def delete(self, key_id: str) -> None:
        self.keys.pop(key_id, None)
