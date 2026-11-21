"""Single sign-on configuration (FR-01.3): the API refuses to start with a provider it
could not use safely, and reads the client secret only through a secret reference."""

from __future__ import annotations

import pytest

from rai.api.oidc import OidcConfig, OidcConfigError
from rai.api.settings import SettingsError, oidc_from_env

GOOD = {
    "RAI_OIDC_ISSUER": "https://login.example.org/realms/rai/",
    "RAI_OIDC_CLIENT_ID": "rai-web",
    "RAI_OIDC_CLIENT_SECRET": "env:TEST_OIDC_SECRET",
    "RAI_OIDC_REDIRECT_URI": "https://rai.example.org/v1/auth/sso/callback",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*GOOD, "RAI_OIDC_UI_URL", "RAI_OIDC_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_OIDC_SECRET", "s3cret-value")
    return monkeypatch


def test_without_an_issuer_sso_is_off(env: pytest.MonkeyPatch) -> None:
    assert OidcConfig.from_env() is None


def test_a_complete_configuration_resolves_the_secret_reference(env: pytest.MonkeyPatch) -> None:
    for name, value in GOOD.items():
        env.setenv(name, value)
    config = OidcConfig.from_env()
    assert config is not None
    assert config.issuer == "https://login.example.org/realms/rai"  # trailing slash dropped
    assert config.client_secret == "s3cret-value"  # noqa: S105 - a test value
    assert "s3cret-value" not in repr(config)
    assert config.secure_cookie
    assert (config.ui_url, config.name) == ("/", "single sign-on")


def test_a_partial_configuration_stops_the_api(env: pytest.MonkeyPatch) -> None:
    env.setenv("RAI_OIDC_ISSUER", GOOD["RAI_OIDC_ISSUER"])
    with pytest.raises(SettingsError, match="RAI_OIDC_CLIENT_ID, RAI_OIDC_CLIENT_SECRET"):
        oidc_from_env()


def test_the_secret_must_be_a_reference(env: pytest.MonkeyPatch) -> None:
    for name, value in GOOD.items():
        env.setenv(name, value)
    env.setenv("RAI_OIDC_CLIENT_SECRET", "the-secret-itself")
    with pytest.raises(SettingsError, match="RAI_OIDC_CLIENT_SECRET"):
        oidc_from_env()


@pytest.mark.parametrize(
    ("field", "value"),
    [("issuer", "http://login.example.org/realms/rai"),
     ("issuer", "https://login.example.org/realms/rai#x"),
     ("redirect_uri", "http://rai.example.org/v1/auth/sso/callback"),
     ("redirect_uri", "https://rai.example.org/cb?next=evil"),
     ("ui_url", "//evil.example.org/"),
     ("ui_url", "http://evil.example.org/")],
)  # fmt: skip
def test_an_unsafe_url_is_refused(field: str, value: str) -> None:
    values = {"issuer": "https://idp.example.org", "client_id": "c", "client_secret": "s",
              "redirect_uri": "https://rai.example.org/v1/auth/sso/callback"}  # fmt: skip
    values[field] = value
    with pytest.raises(OidcConfigError):
        OidcConfig(**values)


def test_http_is_allowed_only_for_localhost() -> None:
    local = OidcConfig(issuer="http://localhost:8180/realms/rai", client_id="c",
                       client_secret="s",  # noqa: S106 - a test value
                       redirect_uri="http://127.0.0.1:8000/v1/auth/sso/callback")  # fmt: skip
    assert not local.secure_cookie
