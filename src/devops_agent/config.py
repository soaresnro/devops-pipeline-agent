"""Settings carregadas de variaveis de ambiente / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    azdo_org: str = Field(..., description="Organizacao Azure DevOps (allowlist).")
    azdo_auth_mode: Literal["pat", "sp"] = "pat"
    azdo_pat: str | None = None
    azdo_sp_tenant_id: str | None = None
    azdo_sp_client_id: str | None = None
    azdo_sp_client_secret: str | None = None

    devops_templates_repo_url: str
    devops_templates_tag: str = "v1"
    devops_templates_reviewers: str = "devops-team"

    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str = "gpt-4.1"
    azure_openai_api_version: str = "2024-10-21"

    log_level: str = "INFO"
    workdir_root: Path = Path("./.tmp/sessions")
    session_ttl_seconds: int = 3600

    @property
    def llm_enabled(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @model_validator(mode="after")
    def _validate_sp_mode(self) -> "Settings":
        # No modo sp, o Service Principal precisa de tenant/client; o client_secret
        # e exigido para o fluxo OBO (check_user_permission do usuario humano).
        if self.azdo_auth_mode == "sp":
            faltando = [
                nome
                for nome, valor in (
                    ("AZDO_SP_TENANT_ID", self.azdo_sp_tenant_id),
                    ("AZDO_SP_CLIENT_ID", self.azdo_sp_client_id),
                    ("AZDO_SP_CLIENT_SECRET", self.azdo_sp_client_secret),
                )
                if not valor
            ]
            if faltando:
                raise ValueError(
                    "Modo de auth 'sp' exige: " + ", ".join(faltando) + "."
                )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings_cache() -> None:
    """Util para testes."""
    global _settings
    _settings = None
