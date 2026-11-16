"""Step 5: per-install API token lives in the OS credential store (rules 8 and 11)."""

from __future__ import annotations

from backend.auth.token import (
    SERVICE_NAME,
    TOKEN_KEY,
    get_or_create_token,
    rotate_token,
    token_matches,
)


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password


def test_token_is_created_once_and_reused() -> None:
    store = FakeStore()
    first = get_or_create_token(store)
    assert len(first) >= 40
    assert store.data[(SERVICE_NAME, TOKEN_KEY)] == first
    assert get_or_create_token(store) == first


def test_each_install_gets_a_different_token() -> None:
    assert get_or_create_token(FakeStore()) != get_or_create_token(FakeStore())


def test_rotate_replaces_the_token() -> None:
    store = FakeStore()
    old = get_or_create_token(store)
    new = rotate_token(store)
    assert new != old
    assert get_or_create_token(store) == new


def test_token_matches_is_exact() -> None:
    assert token_matches("abc123", "abc123")
    assert not token_matches("abc123", "abc124")
    assert not token_matches("abc123", "")
    assert not token_matches("abc123", None)
    assert not token_matches("abc123", "ABC123")
    assert not token_matches("", "")
