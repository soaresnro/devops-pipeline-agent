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
    # App publico para login interativo (device code) que obtem a assercao do
    # usuario para o OBO. Distinto do SP confidencial acima.
    azdo_login_client_id: str | None = None
    # Escopo a pedir no login (audience = app confidencial do agente). Default:
    # "{azdo_sp_client_id}/.default".
    azdo_agent_api_scope: str | None = None

    devops_templates_repo_url: str
    devops_templates_tag: str = "v1"
    # Template de pipeline (no devops-templates) por linguagem detectada. Quando uma
    # linguagem nao tem caminho proprio, usa devops_template_default. web reaproveita
    # o de node (build Node -> nginx).
    devops_template_default: str = "pipelines/k8s-app.yaml"
    devops_template_dotnet: str | None = None
    devops_template_node: str | None = None
    devops_template_python: str | None = None
    devops_template_go: str | None = None
    # Ambientes para os quais gerar helm/environments/<env>/values.yaml (override por env).
    devops_environments: str = "dev,qa,uat,prod"
    # Emite `language` em extends.parameters da pipeline. So ative apos a tag do
    # devops-templates (DEVOPS_TEMPLATES_TAG) declarar esse parametro, senao a
    # compilacao da pipeline gerada falha com "Unexpected parameter 'language'".
    pipeline_pass_language: bool = False

    log_level: str = "INFO"
    workdir_root: Path = Path("./.tmp/sessions")
    session_ttl_seconds: int = 3600

    @property
    def environments(self) -> list[str]:
        return [e.strip() for e in self.devops_environments.split(",") if e.strip()]

    @model_validator(mode="after")
    def _validate_sp_mode(self) -> "Settings":
        # No modo sp, o Service Principal precisa de tenant/client. O client_secret
        # e opcional: sem ele, o app token e o OBO usam a Managed Identity
        # (federated identity credential), sem secret no Key Vault.
        if self.azdo_auth_mode == "sp":
            faltando = [
                nome
                for nome, valor in (
                    ("AZDO_SP_TENANT_ID", self.azdo_sp_tenant_id),
                    ("AZDO_SP_CLIENT_ID", self.azdo_sp_client_id),
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
