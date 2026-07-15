from __future__ import annotations

import pytest

from auth.credentials import InMemoryCredentialStore
from auth.site_credentials import (
    get_or_create_site_credential,
    get_site_credential,
    save_site_credential,
    site_service,
)


URL = "https://career17.sapsf.com/careers?company=scotiabank"


def test_site_service_is_host_and_tenant_scoped() -> None:
    assert site_service(URL, "Scotiabank") == (
        "jobops.ats.career17.sapsf.com.scotiabank"
    )
    assert site_service(URL, "Another Employer") != site_service(URL, "Scotiabank")


@pytest.mark.parametrize(
    "url",
    (
        "http://career17.sapsf.com/careers",
        "https://localhost/register",
        "https://127.0.0.1/register",
        "https://user:secret@career17.sapsf.com/register",
    ),
)
def test_site_service_rejects_unsafe_origins(url: str) -> None:
    with pytest.raises(ValueError):
        site_service(url, "Scotiabank")


def test_get_or_create_persists_and_reuses_strong_password() -> None:
    store = InMemoryCredentialStore()
    first = get_or_create_site_credential(
        URL,
        "Scotiabank",
        "candidate@example.com",
        store=store,
    )
    assert first.created is True
    assert len(first.password) == 24
    assert any(value.isupper() for value in first.password)
    assert any(value.islower() for value in first.password)
    assert any(value.isdigit() for value in first.password)
    assert any(value in "!@#$%^&*_-+=" for value in first.password)

    second = get_or_create_site_credential(
        URL,
        "Scotiabank",
        "candidate@example.com",
        store=store,
    )
    assert second.created is False
    assert second.password == first.password


def test_save_and_load_never_require_secret_outside_store() -> None:
    store = InMemoryCredentialStore()
    saved = save_site_credential(
        URL,
        "Scotiabank",
        "candidate@example.com",
        "Example-password-9!",
        store=store,
    )
    loaded = get_site_credential(
        URL,
        "Scotiabank",
        "candidate@example.com",
        store=store,
    )
    assert loaded is not None
    assert loaded.password == saved.password
    assert "Example-password" not in repr(loaded)
