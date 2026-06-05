"""Testes da aquisicao de tokens Entra ID (azure-identity mockado por monkeypatch)."""

from __future__ import annotations

import pytest
from azure.core.exceptions import ClientAuthenticationError

from devops_agent.config import Settings
from devops_agent.tools import auth


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCred:
    """Credencial fake que devolve um token fixo; registra construcao e scope."""

    last_kwargs: dict[str, object] = {}
    last_scope: str | None = None

    def __init__(self, **kwargs: object) -> None:
        type(self).last_kwargs = kwargs

    def get_token(self, scope: str) -> _FakeToken:
        type(self).last_scope = scope
        return _FakeToken("fake-token")


class _FailingCred:
    def __init__(self, **kwargs: object) -> None:
        pass

    def get_token(self, scope: str) -> _FakeToken:
        raise ClientAuthenticationError("sem credencial")


@pytest.fixture
def sp_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        azdo_org="contoso",
        azdo_auth_mode="sp",
        azdo_sp_tenant_id="tenant",
        azdo_sp_client_id="client",
        azdo_sp_client_secret="secret",
        devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
    )


class TestAcquireAppToken:
    def test_usa_client_secret_quando_ha_secret(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings
    ) -> None:
        monkeypatch.setattr(auth, "ClientSecretCredential", _FakeCred)
        r = auth.acquire_app_token(sp_settings)
        assert r.ok
        assert r.data == "fake-token"
        assert _FakeCred.last_kwargs["client_secret"] == "secret"

    def test_usa_default_credential_sem_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(  # type: ignore[call-arg]
            azdo_org="contoso",
            azdo_auth_mode="pat",  # evita o validator de sp que exige secret
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )
        used: list[str] = []

        class _DAC(_FakeCred):
            def __init__(self, **kwargs: object) -> None:
                used.append("default")
                super().__init__(**kwargs)

        monkeypatch.setattr(auth, "DefaultAzureCredential", _DAC)
        r = auth.acquire_app_token(settings)
        assert r.ok
        assert used == ["default"]

    def test_falha_retorna_tool_result(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings
    ) -> None:
        monkeypatch.setattr(auth, "ClientSecretCredential", _FailingCred)
        r = auth.acquire_app_token(sp_settings)
        assert not r.ok
        assert r.error_code == "auth.app_token"


class TestAcquireUserAssertionInteractive:
    def _sp_with_login(self) -> Settings:
        return Settings(  # type: ignore[call-arg]
            azdo_org="contoso",
            azdo_auth_mode="sp",
            azdo_sp_tenant_id="tenant",
            azdo_sp_client_id="conf-app",
            azdo_sp_client_secret="secret",
            azdo_login_client_id="public-cli",
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )

    def test_sucesso_usa_scope_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth, "DeviceCodeCredential", _FakeCred)
        r = auth.acquire_user_assertion_interactive(self._sp_with_login())
        assert r.ok
        assert r.data == "fake-token"
        assert _FakeCred.last_kwargs["client_id"] == "public-cli"
        # scope default derivado do app confidencial
        assert _FakeCred.last_scope == "conf-app/.default"

    def test_sem_login_client_falha(self, sp_settings: Settings) -> None:
        # sp_settings nao tem azdo_login_client_id
        r = auth.acquire_user_assertion_interactive(sp_settings)
        assert not r.ok
        assert r.error_code == "auth.login_not_configured"

    def test_falha_credencial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth, "DeviceCodeCredential", _FailingCred)
        r = auth.acquire_user_assertion_interactive(self._sp_with_login())
        assert not r.ok
        assert r.error_code == "auth.interactive_login"


class TestAcquireUserOboToken:
    def test_sucesso_com_secret(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings
    ) -> None:
        monkeypatch.setattr(auth, "OnBehalfOfCredential", _FakeCred)
        r = auth.acquire_user_obo_token(sp_settings, "user-jwt")
        assert r.ok
        assert r.data == "fake-token"
        assert _FakeCred.last_kwargs["user_assertion"] == "user-jwt"
        assert _FakeCred.last_kwargs.get("client_secret") == "secret"
        assert "client_assertion_func" not in _FakeCred.last_kwargs

    def test_sucesso_sem_secret_usa_managed_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(  # type: ignore[call-arg]
            azdo_org="contoso",
            azdo_auth_mode="sp",
            azdo_sp_tenant_id="tenant",
            azdo_sp_client_id="client",
            # sem azdo_sp_client_secret -> caminho federado via MI
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )
        monkeypatch.setattr(auth, "OnBehalfOfCredential", _FakeCred)
        monkeypatch.setattr(auth, "DefaultAzureCredential", _FakeCred)
        r = auth.acquire_user_obo_token(settings, "user-jwt")
        assert r.ok
        assert "client_secret" not in _FakeCred.last_kwargs
        func = _FakeCred.last_kwargs["client_assertion_func"]
        assert callable(func)
        # a Managed Identity emite a client assertion sob demanda
        assert func() == "fake-token"

    def test_sem_assertion_falha(self, sp_settings: Settings) -> None:
        r = auth.acquire_user_obo_token(sp_settings, "")
        assert not r.ok
        assert r.error_code == "auth.obo_missing_assertion"

    def test_falha_credencial(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings
    ) -> None:
        monkeypatch.setattr(auth, "OnBehalfOfCredential", _FailingCred)
        r = auth.acquire_user_obo_token(sp_settings, "user-jwt")
        assert not r.ok
        assert r.error_code == "auth.obo"
