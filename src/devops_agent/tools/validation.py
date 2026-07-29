"""Tools de validacao de inputs do usuario.

Todas as tools retornam ToolResult padronizado e tratam excecoes internamente -
nunca propagam exception para o LLM ou orquestrador.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from devops_agent.models import AppType, Language, ToolResult
from devops_agent.models.inputs import RepoCoordinates

# Aliases comuns que o usuario pode digitar para cada linguagem.
_LANGUAGE_ALIASES: dict[str, Language] = {
    "dotnet": Language.DOTNET,
    ".net": Language.DOTNET,
    "net": Language.DOTNET,
    "csharp": Language.DOTNET,
    "c#": Language.DOTNET,
    "node": Language.NODE,
    "nodejs": Language.NODE,
    "node.js": Language.NODE,
    "js": Language.NODE,
    "javascript": Language.NODE,
    "typescript": Language.NODE,
    "ts": Language.NODE,
    "python": Language.PYTHON,
    "py": Language.PYTHON,
    "go": Language.GO,
    "golang": Language.GO,
}

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

# Formato legado (org no subdominio):
#   https://{org}.visualstudio.com/{project}/_git/{repo}
#   https://{org}@{org}.visualstudio.com/{project}/_git/{repo}
_AZDO_VSTS_URL_RE = re.compile(
    r"^https://(?:[^@]+@)?(?P<org>[^.@/]+)\.visualstudio\.com/(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)/?$",
    re.IGNORECASE,
)

# Branch git: regras simplificadas (suficientes para 99% dos casos).
_BRANCH_RE = re.compile(r"^(?!/|.*//|.*\.\.|.*@\{)[A-Za-z0-9._/\-]+(?<![./])$")

# Cron: validacao pragmatica (5 campos com o charset de cron) + macros do K8s.
_CRON_MACROS: frozenset[str] = frozenset(
    {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
)
_CRON_FIELD_RE = re.compile(r"^[\d*/,\-]+$")

# Versao .NET: banda com feature (8.0.x) ou patch pinado (8.0.100). Default 8.0.x.
_DOTNET_VERSION_RE = re.compile(r"^(6|7|8|9)\.0\.(x|\d{1,3})$")
_DEFAULT_DOTNET_VERSION = "8.0.x"

# Assembly de entrada .NET (ex.: ePharma.Authentication.Services[.dll]).
_DOTNET_ENTRYPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Versao Python: minor (3.12) ou patch pinado (3.12.4). Vira o versionSpec do
# UsePythonVersion e a tag da imagem base (python:<versao>-alpine).
_PYTHON_VERSION_RE = re.compile(r"^3\.(?:8|9|1[0-9])(?:\.\d{1,2})?$")
_DEFAULT_PYTHON_VERSION = "3.12"

# Gerenciadores aceitos pelo template python (parametro packageManager).
_PYTHON_PACKAGE_MANAGERS: dict[str, str] = {
    "pip": "pip",
    "requirements": "pip",
    "requirements.txt": "pip",
    "poetry": "poetry",
    "pyproject": "poetry",
    "pyproject.toml": "poetry",
}

# Metacaracteres de shell: o CMD do Dockerfile e emitido em exec form (lista JSON),
# entao nao ha shell para interpreta-los - recusamos para o usuario nao ser surpreendido.
_SHELL_METACHARS = frozenset("|&;<>$`\\\"'()[]{}\n\r")

# Token do comando de entrada: pode comecar com '-' (flags, ex.: --host).
_PYTHON_ENTRYPOINT_TOKEN_RE = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9._:/=,+-]*$")

# Label DNS-1123 (applicationName, path de APIM, host base do ingress): minusculas,
# digitos e hifen; nao comeca/termina com hifen. Reaproveita a regra do namespace.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS_LABEL_MAX_LEN = 63

# Respostas sim/nao aceitas (o CLI ja usa esse conjunto no CONFIRM).
_YES: frozenset[str] = frozenset({"sim", "s", "yes", "y", "true", "1"})
_NO: frozenset[str] = frozenset({"nao", "não", "n", "no", "false", "0"})

# apiId do APIM: identificador com letras/digitos e . _ - (comeca com letra/digito).
_API_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Ambientes do APIM na ORDEM em que o usuario informa os apiIds e em que a pipeline
# declara os parametros (apiIdDev/apiIdQa/apiIdUat/apiIdProd). Fixo pelo contrato do
# template-aks - nao confundir com DEVOPS_ENVIRONMENTS, que controla os overrides do Helm.
APIM_ENVIRONMENTS: tuple[str, ...] = ("dev", "qa", "uat", "prod")


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

    clean = url.strip()
    match = _AZDO_URL_RE.match(clean) or _AZDO_VSTS_URL_RE.match(clean)
    if not match:
        return ToolResult.failure(
            "Formato nao reconhecido. Use o link de clone HTTPS do Azure DevOps: "
            "https://dev.azure.com/<org>/<projeto>/_git/<repo> "
            "ou https://<org>.visualstudio.com/<projeto>/_git/<repo>",
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


def validate_language(value: str) -> ToolResult[Language]:
    """Normaliza e valida a linguagem, aceitando aliases comuns."""
    if not value or not isinstance(value, str):
        return ToolResult.failure("Linguagem nao informada.", code="language.empty")

    normalized = value.strip().lower()
    lang = _LANGUAGE_ALIASES.get(normalized)
    if lang is None:
        try:
            lang = Language(normalized)
        except ValueError:
            accepted = ", ".join(t.value for t in Language)
            return ToolResult.failure(
                f"Linguagem '{value}' nao reconhecida. Use uma de: {accepted}.",
                code="language.invalid",
            )

    return ToolResult.success(lang)


def validate_cron_schedule(value: str) -> ToolResult[str]:
    """Valida uma expressao cron (5 campos) ou um macro do K8s (ex.: @daily)."""
    if not value or not isinstance(value, str):
        return ToolResult.failure("Agendamento (cron) nao informado.", code="cron.empty")

    cron = " ".join(value.split())  # normaliza espacos

    if cron in _CRON_MACROS:
        return ToolResult.success(cron)

    fields = cron.split(" ")
    if len(fields) != 5:
        return ToolResult.failure(
            "Cron deve ter 5 campos (minuto hora dia mes dia-da-semana), "
            "ex.: '0 2 * * *'. Ou use um macro como @daily.",
            code="cron.bad_field_count",
        )
    if not all(_CRON_FIELD_RE.match(f) for f in fields):
        return ToolResult.failure(
            "Cron invalido. Use apenas digitos e os caracteres * , - / em cada campo.",
            code="cron.invalid_format",
        )
    return ToolResult.success(cron)


def validate_dotnet_version(value: str) -> ToolResult[str]:
    """Valida a versao .NET (ex.: 8.0.x ou 8.0.100). Vazio -> default 8.0.x."""
    if not value or not value.strip():
        return ToolResult.success(_DEFAULT_DOTNET_VERSION)

    version = value.strip().lower().removeprefix("v")
    if not _DOTNET_VERSION_RE.match(version):
        return ToolResult.failure(
            "Versao .NET invalida. Use o formato <major>.0.x (ex.: 8.0.x) ou um patch "
            "(ex.: 8.0.100). Suportados: 6, 7, 8, 9.",
            code="dotnet_version.invalid",
        )
    return ToolResult.success(version)


def validate_dotnet_entrypoint(value: str) -> ToolResult[str]:
    """Valida o assembly de entrada .NET (ex.: ePharma.Auth.dll). Adiciona .dll se faltar."""
    if not value or not value.strip():
        return ToolResult.failure("Entrypoint nao informado.", code="dotnet_entrypoint.empty")

    name = value.strip()
    if not _DOTNET_ENTRYPOINT_RE.match(name):
        return ToolResult.failure(
            "Nome de assembly invalido. Use letras, digitos e os caracteres . _ - "
            "(ex.: ePharma.Authentication.Services.dll).",
            code="dotnet_entrypoint.invalid",
        )
    if not name.lower().endswith(".dll"):
        name += ".dll"
    return ToolResult.success(name)


def validate_python_version(value: str) -> ToolResult[str]:
    """Valida a versao do Python (ex.: 3.12 ou 3.12.4). Vazio -> default 3.12."""
    if not value or not value.strip():
        return ToolResult.success(_DEFAULT_PYTHON_VERSION)

    version = value.strip().lower().removeprefix("v").removeprefix("python").strip()
    if not _PYTHON_VERSION_RE.match(version):
        return ToolResult.failure(
            "Versao Python invalida. Use <major>.<minor> (ex.: 3.12) ou um patch "
            "(ex.: 3.12.4). Suportadas: 3.8 a 3.19.",
            code="python_version.invalid",
        )
    return ToolResult.success(version)


def validate_python_package_manager(value: str) -> ToolResult[str]:
    """Valida o packageManager do template python (pip ou poetry). Vazio -> pip."""
    if not value or not value.strip():
        return ToolResult.success("pip")

    manager = _PYTHON_PACKAGE_MANAGERS.get(value.strip().lower())
    if manager is None:
        return ToolResult.failure(
            "Gerenciador invalido. Use 'pip' (requirements.txt) ou 'poetry' (pyproject.toml).",
            code="python_package_manager.invalid",
        )
    return ToolResult.success(manager)


def validate_python_entrypoint(value: str) -> ToolResult[str]:
    """Valida o comando de entrada da imagem Python (vira o CMD em exec form).

    Ex.: 'uvicorn main:app --host 0.0.0.0 --port 8080' ou 'python -m app.worker'.
    """
    if not value or not value.strip():
        return ToolResult.failure(
            "Entrypoint nao informado.", code="python_entrypoint.empty"
        )

    command = " ".join(value.split())
    if any(ch in _SHELL_METACHARS for ch in command):
        return ToolResult.failure(
            "Entrypoint invalido: o CMD e gerado em exec form (sem shell), entao aspas, "
            "pipes, redirecionamentos e variaveis ($VAR) nao sao interpretados. "
            "Informe apenas o comando e seus argumentos "
            "(ex.: uvicorn main:app --host 0.0.0.0 --port 8080).",
            code="python_entrypoint.shell_syntax",
        )
    if not all(_PYTHON_ENTRYPOINT_TOKEN_RE.match(token) for token in command.split(" ")):
        return ToolResult.failure(
            "Entrypoint invalido. Use letras, digitos e os caracteres . _ - : / = , + "
            "(ex.: gunicorn app.main:app --bind 0.0.0.0:8080).",
            code="python_entrypoint.invalid",
        )
    return ToolResult.success(command)


def validate_yes_no(value: str) -> ToolResult[bool]:
    """Interpreta uma resposta sim/nao. Nao assume default: vazio e recusado."""
    if not value or not value.strip():
        return ToolResult.failure("Responda sim ou nao.", code="yes_no.empty")

    resposta = value.strip().lower()
    if resposta in _YES:
        return ToolResult.success(True)
    if resposta in _NO:
        return ToolResult.success(False)
    return ToolResult.failure(
        f"Resposta '{value.strip()}' nao reconhecida. Responda sim ou nao.",
        code="yes_no.invalid",
    )


def validate_application_name(value: str) -> ToolResult[str]:
    """Valida o applicationName da pipeline/Helm (label DNS-1123, ex.: api-utils)."""
    if not value or not value.strip():
        return ToolResult.failure("applicationName nao informado.", code="application_name.empty")
    name = value.strip().lower()
    if len(name) > _DNS_LABEL_MAX_LEN or not _DNS_LABEL_RE.match(name):
        return ToolResult.failure(
            "applicationName invalido. Use minusculas, digitos e hifen; "
            "comece e termine com letra ou digito (ex.: api-utils).",
            code="application_name.invalid",
        )
    return ToolResult.success(name)


def validate_apim_path(value: str) -> ToolResult[str]:
    """Valida o pathApiManagementService (label DNS-1123, ex.: utils)."""
    if not value or not value.strip():
        return ToolResult.failure(
            "pathApiManagementService nao informado.", code="apim_path.empty"
        )
    path = value.strip().lower()
    if len(path) > _DNS_LABEL_MAX_LEN or not _DNS_LABEL_RE.match(path):
        return ToolResult.failure(
            "pathApiManagementService invalido. Use minusculas, digitos e hifen (ex.: utils).",
            code="apim_path.invalid",
        )
    return ToolResult.success(path)


def validate_api_ids(value: str, *, single: bool = False) -> ToolResult[dict[str, str]]:
    """Valida os apiIds do APIM, um por ambiente (obrigatorio, sem default).

    Aceita duas formas: um valor unico (aplicado aos quatro ambientes) ou os quatro
    separados por virgula, na ordem de `APIM_ENVIRONMENTS` (dev, qa, uat, prod).
    Cada apiId aceita letras, digitos e os caracteres . _ - (ex.: api-utils, utils-v1).

    `single=True` restringe ao valor unico: o template-aks-python declara um unico
    parametro `apiId`, entao aceitar quatro daria a impressao falsa de que os
    ambientes teriam apiIds distintos.
    """
    if not value or not value.strip():
        return ToolResult.failure("apiIds nao informados.", code="api_ids.empty")

    partes = [parte.strip() for parte in value.split(",")]
    if len(partes) == 1:
        partes = partes * len(APIM_ENVIRONMENTS)
    elif single:
        return ToolResult.failure(
            "Informe um unico apiId: o template python usa o mesmo em todos os ambientes "
            f"(recebido: {len(partes)}).",
            code="api_ids.single_only",
        )
    elif len(partes) != len(APIM_ENVIRONMENTS):
        return ToolResult.failure(
            f"Informe 1 apiId (o mesmo para todos os ambientes) ou {len(APIM_ENVIRONMENTS)} "
            f"separados por virgula na ordem {', '.join(APIM_ENVIRONMENTS)} "
            f"(recebido: {len(partes)}).",
            code="api_ids.count",
        )

    api_ids: dict[str, str] = {}
    for env, api_id in zip(APIM_ENVIRONMENTS, partes, strict=True):
        if not api_id:
            return ToolResult.failure(
                f"apiId de {env} nao informado (nao deixe posicoes vazias entre as virgulas).",
                code="api_ids.empty_item",
            )
        if len(api_id) > 120 or not _API_ID_RE.match(api_id):
            return ToolResult.failure(
                f"apiId de {env} invalido ('{api_id}'). Use letras, digitos e os caracteres "
                ". _ - (comece com letra ou digito; ex.: api-utils).",
                code="api_ids.invalid",
            )
        api_ids[env] = api_id
    return ToolResult.success(api_ids)


def validate_ingress_host(value: str) -> ToolResult[str]:
    """Valida o host base do ingress (label DNS-1123 sem pontos, ex.: servicesutils).

    O dominio completo (.k8s.<env>.epharma.com.br e o legado) e derivado por ambiente.
    """
    if not value or not value.strip():
        return ToolResult.failure("Host do ingress nao informado.", code="ingress_host.empty")
    host = value.strip().lower()
    if len(host) > _DNS_LABEL_MAX_LEN or not _DNS_LABEL_RE.match(host):
        return ToolResult.failure(
            "Host base invalido. Use minusculas, digitos e hifen, sem pontos "
            "(o dominio e adicionado automaticamente por ambiente; ex.: servicesutils).",
            code="ingress_host.invalid",
        )
    return ToolResult.success(host)


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
