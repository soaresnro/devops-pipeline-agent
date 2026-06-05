"""Aquisicao de tokens Entra ID para autenticar contra o Azure DevOps.

Dois tokens distintos (ADR-001):
- App token: identidade do Service Principal/Managed Identity, usado para clonar,
  fazer push e abrir PR. Via client secret (dev/Key Vault) ou DefaultAzureCredential
  (Managed Identity em Container Apps = o "WIF" do ADR; workload identity em AKS).
- OBO token: identidade do usuario humano, usado SO para check_user_permission.
  Troca o token de assercao do usuario por um token com audience Azure DevOps.
  O app se autentica no OBO via client secret OU, sem secret, via client assertion
  emitido pela Managed Identity (federated identity credential).

As funcoes nunca propagam excecao - sempre retornam ToolResult.
"""

from __future__ import annotations

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    ClientSecretCredential,
    DefaultAzureCredential,
    DeviceCodeCredential,
    OnBehalfOfCredential,
)

from devops_agent.config import Settings
from devops_agent.models import ToolResult

# Resource ID fixo do Azure DevOps; ".default" pede os scopes pre-consentidos.
AZDO_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

# Audience da troca de token federada (Workload Identity): a Managed Identity
# emite um token para este escopo que serve de client assertion do app no OBO.
_TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


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


def acquire_user_assertion_interactive(settings: Settings) -> ToolResult[str]:
    """Login interativo (device code) que obtem a assercao do usuario para o OBO.

    O usuario autentica seguindo o codigo/URL exibidos; o token resultante (com
    audience do app do agente) e usado como user_assertion na troca OBO.
    """
    if not settings.azdo_login_client_id:
        return ToolResult.failure(
            "AZDO_LOGIN_CLIENT_ID nao configurado - login interativo indisponivel.",
            code="auth.login_not_configured",
        )

    scope = settings.azdo_agent_api_scope or f"{settings.azdo_sp_client_id}/.default"
    try:
        credential = DeviceCodeCredential(
            tenant_id=settings.azdo_sp_tenant_id or "",
            client_id=settings.azdo_login_client_id,
        )
        token = credential.get_token(scope)
    except (ClientAuthenticationError, ValueError) as exc:
        return ToolResult.failure(
            f"Falha no login interativo: {exc}",
            code="auth.interactive_login",
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
        if settings.azdo_sp_client_secret:
            credential = OnBehalfOfCredential(
                tenant_id=settings.azdo_sp_tenant_id or "",
                client_id=settings.azdo_sp_client_id or "",
                client_secret=settings.azdo_sp_client_secret,
                user_assertion=user_assertion,
            )
        else:
            # Sem secret: o app se autentica com um client assertion emitido pela
            # Managed Identity (federated identity credential), eliminando o secret.
            mi = DefaultAzureCredential()
            credential = OnBehalfOfCredential(
                tenant_id=settings.azdo_sp_tenant_id or "",
                client_id=settings.azdo_sp_client_id or "",
                client_assertion_func=lambda: mi.get_token(_TOKEN_EXCHANGE_SCOPE).token,
                user_assertion=user_assertion,
            )
        token = credential.get_token(AZDO_RESOURCE_SCOPE)
    except (ClientAuthenticationError, ValueError) as exc:
        return ToolResult.failure(
            f"Falha na troca OBO do token do usuario: {exc}",
            code="auth.obo",
        )
    return ToolResult.success(token.token)
