"""Tools de validacao de inputs do usuario.

Todas as tools retornam ToolResult padronizado e tratam excecoes internamente -
nunca propagam exception para o LLM ou orquestrador.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from devops_agent.models import AppType, ToolResult
from devops_agent.models.inputs import RepoCoordinates

# RFC 1123 label: letras minusculas, digitos, hifens; nao comeca/termina com hifen.
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_NAMESPACE_MAX_LEN = 63

# Namespaces reservados do K8s/AKS que nao devem ser alvo de apps de squad.
_RESERVED_NAMESPACES: frozenset[str] = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gatekeeper-system",
        "azure-arc",
        "azure-extensions-usage-system",
        "istio-system",
        "ingress-nginx",
        "cert-manager",
        "monitoring",
        "prometheus",
        "grafana",
    }
)

# Padroes aceitos de URL de repo Azure DevOps:
#   https://dev.azure.com/{org}/{project}/_git/{repo}
#   https://{org}@dev.azure.com/{org}/{project}/_git/{repo}
_AZDO_URL_RE = re.compile(
    r"^https://(?:[^@]+@)?dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)/?$",
    re.IGNORECASE,
)

# Branch git: regras simplificadas (suficientes para 99% dos casos).
_BRANCH_RE = re.compile(r"^(?!/|.*//|.*\.\.|.*@\{)[A-Za-z0-9._/\-]+(?<![./])$")


def validate_repo_url(
    url: str,
    allowed_org: str,
) -> ToolResult[RepoCoordinates]:
    """Valida que a URL e de um repo Azure DevOps na organizacao permitida."""
    if not url or not isinstance(url, str):
        return ToolResult.failure("URL vazia ou invalida.", code="repo.empty")

    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return ToolResult.failure(
            "A URL precisa usar HTTPS (ex.: https://dev.azure.com/org/project/_git/repo).",
            code="repo.not_https",
        )

    match = _AZDO_URL_RE.match(url.strip())
    if not match:
        return ToolResult.failure(
            "Formato nao reconhecido. Use o link de clone HTTPS do Azure DevOps: "
            "https://dev.azure.com/<org>/<projeto>/_git/<repo>",
            code="repo.bad_format",
        )

    org = match.group("org")
    if org.lower() != allowed_org.lower():
        return ToolResult.failure(
            f"Organizacao '{org}' nao esta na allowlist. "
            f"Apenas repos em '{allowed_org}' sao aceitos.",
            code="repo.org_not_allowed",
        )

    return ToolResult.success(
        RepoCoordinates(
            organization=org,
            project=match.group("project"),
            repository=match.group("repo"),
        )
    )


def validate_k8s_namespace(namespace: str) -> ToolResult[str]:
    """Valida nome de namespace Kubernetes (RFC 1123 + lista de reservados)."""
    if not namespace or not isinstance(namespace, str):
        return ToolResult.failure("Namespace nao pode ser vazio.", code="ns.empty")

    ns = namespace.strip().lower()

    if len(ns) > _NAMESPACE_MAX_LEN:
        return ToolResult.failure(
            f"Namespace excede {_NAMESPACE_MAX_LEN} caracteres.",
            code="ns.too_long",
        )

    if not _NAMESPACE_RE.match(ns):
        return ToolResult.failure(
            "Namespace invalido. Use apenas letras minusculas, digitos e hifen; "
            "deve comecar e terminar com letra ou digito (ex.: payments-prod).",
            code="ns.invalid_format",
        )

    if ns in _RESERVED_NAMESPACES:
        return ToolResult.failure(
            f"Namespace '{ns}' e reservado para o sistema/plataforma.",
            code="ns.reserved",
        )

    if any(ns.startswith(prefix) for prefix in ("kube-", "azure-", "istio-")):
        return ToolResult.failure(
            f"Namespace '{ns}' usa prefixo reservado pela plataforma.",
            code="ns.reserved_prefix",
        )

    return ToolResult.success(ns)


def validate_app_type(value: str) -> ToolResult[AppType]:
    """Normaliza e valida o tipo de aplicacao."""
    if not value or not isinstance(value, str):
        return ToolResult.failure("Tipo de aplicacao nao informado.", code="type.empty")

    normalized = value.strip().lower()
    try:
        return ToolResult.success(AppType(normalized))
    except ValueError:
        accepted = ", ".join(t.value for t in AppType)
        return ToolResult.failure(
            f"Tipo '{value}' nao reconhecido. Use um de: {accepted}.",
            code="type.invalid",
        )


def validate_branch_name(branch: str) -> ToolResult[str]:
    """Validacao leve de nome de branch git."""
    if not branch or not isinstance(branch, str):
        return ToolResult.failure("Branch nao pode ser vazia.", code="branch.empty")

    name = branch.strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/") :]

    if len(name) > 200:
        return ToolResult.failure("Nome da branch excede 200 caracteres.", code="branch.too_long")

    if not _BRANCH_RE.match(name):
        return ToolResult.failure(
            "Nome de branch invalido. Evite espacos, '..', '@{' e barras no inicio/fim.",
            code="branch.invalid_format",
        )

    return ToolResult.success(name)
