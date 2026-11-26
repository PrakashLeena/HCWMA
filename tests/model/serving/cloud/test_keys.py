"""The credential store wrapper: own target per profile, read-back check, key formats.

Uses a fake keyring backend; never the owner's real credential store.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from model.serving.cloud.keys import (
    SERVICE_PREFIX,
    KeyringKeyStore,
    KeyStoreError,
    key_hint,
    normalize_key,
)


class CaseInsensitiveVault(KeyringBackend):
    """Like Windows: one entry per target, target names ignore case."""

    priority = 1  # type: ignore[assignment]

    def __init__(self, lose_writes: bool = False) -> None:
        super().__init__()
        self.entries: dict[str, tuple[str, str]] = {}
        self.lose_writes = lose_writes

    def get_password(self, service: str, username: str) -> str | None:
        entry = self.entries.get(service.lower())
        return entry[1] if entry and entry[0] == username else None

    def set_password(self, service: str, username: str, password: str) -> None:
        if not self.lose_writes:
            self.entries[service.lower()] = (username, password)

    def delete_password(self, service: str, username: str) -> None:
        if self.get_password(service, username) is None:
            raise PasswordDeleteError(service)
        del self.entries[service.lower()]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[CaseInsensitiveVault]:
    monkeypatch.delenv("HCWMA_DISABLE_KEYRING", raising=False)
    before = keyring.get_keyring()
    fake = CaseInsensitiveVault()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(before)


def test_each_profile_has_its_own_target_not_shared_with_gmail(vault: CaseInsensitiveVault) -> None:
    store = KeyringKeyStore()
    vault.set_password("HCWMA", "gmail_account_address", "me@example.com")  # Gmail's entry
    assert store.set("nvidia", "nvapi-abcdefghijklmnop0001") == "nvapi-abcdefghijklmnop0001"
    assert store.set("groq", "gsk_abcdefghijklmnop0002") == "gsk_abcdefghijklmnop0002"
    # a later Gmail write does not touch the cloud keys
    vault.set_password("HCWMA", "gmail_account_address", "me@example.com")
    assert store.get("nvidia") == "nvapi-abcdefghijklmnop0001"
    assert store.get("groq") == "gsk_abcdefghijklmnop0002"
    assert {t for t in vault.entries if t != "hcwma"} == {
        (SERVICE_PREFIX + "nvidia").lower(),
        (SERVICE_PREFIX + "groq").lower(),
    }
    assert vault.entries["hcwma"] == ("gmail_account_address", "me@example.com")
    store.delete("nvidia")
    assert store.get("nvidia") is None and store.get("groq") is not None


def test_a_write_that_does_not_read_back_is_an_error(vault: CaseInsensitiveVault) -> None:
    vault.lose_writes = True
    with pytest.raises(KeyStoreError, match="read back"):
        KeyringKeyStore().set("nvidia", "nvapi-abcdefghijklmnop0001")


def test_errors_never_contain_the_key(vault: CaseInsensitiveVault) -> None:
    key = "nvapi-secret value-0001"
    with pytest.raises(KeyStoreError) as err:
        KeyringKeyStore().set("nvidia", key)
    assert key not in str(err.value)
    with pytest.raises(KeyStoreError):
        KeyringKeyStore().set("../escape", "nvapi-abcdefghijklmnop0001")


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("nvapi-AbC_123-xyz789", "nvapi-AbC_123-xyz789"),
        ("  Bearer sk-proj-abcdefgh1234  ", "sk-proj-abcdefgh1234"),
        ("bearer gsk_abcdefgh1234", "gsk_abcdefgh1234"),
        ('"AIzaSyAbCdEfGh123456"', "AIzaSyAbCdEfGh123456"),
    ],
)
def test_key_formats(raw: str, key: str) -> None:
    assert normalize_key(raw) == key


@pytest.mark.parametrize("raw", ["", "short", "two words here", "Bearer "])
def test_bad_keys(raw: str) -> None:
    with pytest.raises(KeyStoreError):
        normalize_key(raw)


def test_hint_shows_only_the_end() -> None:
    assert key_hint("nvapi-abcdefgh1234") == "…1234"
    assert key_hint(None) is None
