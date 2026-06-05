"""Aquisicao de tokens Entra ID para autenticar contra o Azure DevOps.

Dois tokens distintos (ADR-001):
- App token: identidade do Service Principal/Managed Identity, usado para clonar,
  fazer push e abrir PR. Via client secret (dev/Key Vault) ou DefaultAzureCredential
  (Managed Identity em Container Apps = o "WIF" do ADR; workload identity em AKS).
- OBO token: identidade do usuario humano, usado SO para check_user_permission.
  Troca o token de assercao do usuario por um token com audience Azure DevOps.

As funcoes nunca propagam excecao - sempre retornam ToolResult.
"""

from __future__ import annotations

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    ClientSecretCredential,
    DefaultAzureCredential,
    OnBehalfOfCredential,
)

from devops_agent.config import Settings
from devops_agent.models import ToolResult

# Resource ID fixo do Azure DevOps; ".default" pede os scopes pre-consentidos.
AZDO_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


def acquire_app_token(settings: Settings) -> ToolResult[str]:
    """Token da identidade da aplicacao (SP) para operacoes de git/PR.

    Usa ClientSecretCredential quando ha secret; senao DefaultAzureCredential
    (Managed Identity em Container Apps, workload identity em AKS, az-cli em dev).
    """
    credential: TokenCredential
    try:
        if settings.azdo_sp_client_secret:
            credential = ClientSecretCredential(
                tenant_id=settings.azdo_sp_tenant_id or "",
                client_id=settings.azdo_sp_client_id or "",
                client_secret=settings.azdo_sp_client_secret,
            )
        else:
            credential = DefaultAzureCredential()
        token = credential.get_token(AZDO_RESOURCE_SCOPE)
    except (ClientAuthenticationError, ValueError) as exc:
        return ToolResult.failure(
            f"Falha adquirindo token do Service Principal: {exc}",
            code="auth.app_token",
        )
    return ToolResult.success(token.token)


def acquire_user_obo_token(settings: Settings, user_assertion: str) -> ToolResult[str]:
    """Token OBO (on-behalf-of) com a identidade do usuario humano.

    Troca o token de assercao do usuario (aud = app do agente) por um token
    com audience Azure DevOps, preservando a identidade do usuario.
    """
    if not user_assertion:
        return ToolResult.failure(
            "Token de assercao do usuario ausente - OBO impossivel.",
            code="auth.obo_missing_assertion",
        )
    try:
        credential = OnBehalfOfCredential(
            tenant_id=settings.azdo_sp_tenant_id or "",
            client_id=settings.azdo_sp_client_id or "",
            client_secret=settings.azdo_sp_client_secret or "",
            user_assertion=user_assertion,
        )
        token = credential.get_token(AZDO_RESOURCE_SCOPE)
    except (ClientAuthenticationError, ValueError) as exc:
        return ToolResult.failure(
            f"Falha na troca OBO do token do usuario: {exc}",
            code="auth.obo",
        )
    return ToolResult.success(token.token)
