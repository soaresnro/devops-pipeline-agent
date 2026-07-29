"""Tool de scaffold: renderiza templates Jinja2 e monta o bundle de arquivos.

Esta tool e a parte DETERMINISTICA da geracao - o LLM nao escreve YAML ou Dockerfile.
Ele apenas decide *quando* chamar render/dry_run e com quais parametros tipados.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from devops_agent.models import AppType, Language, ToolResult
from devops_agent.tools.validation import APIM_ENVIRONMENTS


class ScaffoldedFile(BaseModel):
    """Um arquivo a ser escrito no repositorio da aplicacao.

    Texto por padrao (`content`). Para artefatos binarios (ex.: o `.tgz` da base
    chart vendorizada), `binary_b64` carrega os bytes em base64 e `content` fica
    vazio; `write_files` decide entre `write_text`/`write_bytes` por `is_binary`.
    """

    path: str
    content: str = ""
    binary_b64: str | None = None

    @property
    def is_binary(self) -> bool:
        return self.binary_b64 is not None


class ScaffoldBundle(BaseModel):
    """Conjunto completo de arquivos resultantes do scaffold.

    Tudo vai para o repositorio da PROPRIA aplicacao (Dockerfile,
    azure-pipelines.yaml e o Helm em helm/base + helm/environments).
    """

    files: list[ScaffoldedFile]

    @property
    def all_paths(self) -> list[str]:
        return [f.path for f in self.files]


class DryRunResult(BaseModel):
    """Resultado da pre-renderizacao com colisoes detectadas."""

    bundle: ScaffoldBundle
    collisions: list[str] = []

    @property
    def has_collisions(self) -> bool:
        return bool(self.collisions)


@dataclass(frozen=True)
class TypeDefaults:
    """Defaults por tipo de aplicacao, injetados no values.yaml do Helm."""

    container_port: int
    service_port: int
    health_path: str
    replica_count: int
    requests_cpu: str
    requests_memory: str
    limits_cpu: str
    limits_memory: str


_TYPE_DEFAULTS: dict[AppType, TypeDefaults] = {
    AppType.API: TypeDefaults(
        container_port=8080,
        service_port=80,
        health_path="/health",
        replica_count=2,
        requests_cpu="250m",
        requests_memory="512Mi",
        limits_cpu="1000m",
        limits_memory="1Gi",
    ),
    AppType.WORKER: TypeDefaults(
        container_port=0,
        service_port=0,
        health_path="",
        replica_count=1,
        requests_cpu="250m",
        requests_memory="512Mi",
        limits_cpu="1000m",
        limits_memory="1Gi",
    ),
    AppType.WEB: TypeDefaults(
        container_port=80,
        service_port=80,
        health_path="/",
        replica_count=2,
        requests_cpu="100m",
        requests_memory="256Mi",
        limits_cpu="500m",
        limits_memory="512Mi",
    ),
    # CronJob nao expoe porta nem mantem replicas; campos de rede/replica ficam zerados
    # e nao sao renderizados no values.yaml (ver condicionais em _common/values.yaml.j2).
    AppType.CRONJOB: TypeDefaults(
        container_port=0,
        service_port=0,
        health_path="",
        replica_count=0,
        requests_cpu="100m",
        requests_memory="256Mi",
        limits_cpu="500m",
        limits_memory="512Mi",
    ),
}


# Agendamento cron padrao para cronjob quando o usuario nao informa um (de hora em hora).
DEFAULT_CRON_SCHEDULE = "0 * * * *"

# Caminho default do template de pipeline no repo devops-templates.
DEFAULT_PIPELINE_TEMPLATE_PATH = "pipelines/k8s-app.yaml"

# Branch default do repo de templates referenciado pelo stack dotnet (contrato template-aks).
DEFAULT_TEMPLATES_BRANCH = "master"

# Versao default do .NET quando o usuario nao informa.
DEFAULT_DOTNET_VERSION = "8.0.x"

# Defaults do stack python (contrato template-aks-python).
DEFAULT_PYTHON_VERSION = "3.12"
DEFAULT_PYTHON_PACKAGE_MANAGER = "pip"

# Ambientes default para os quais gerar override de values.
DEFAULT_ENVIRONMENTS = ("dev", "qa", "uat", "prod")

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"

# Base charts vendorizadas EMPACOTADAS (<base>-<versao>.tgz) + Chart.lock, saidas reais
# de `helm dependency build` do repo modelo do time (AksHelmBaseCharts). O agente emite o
# par consistente (.tgz em charts/ + Chart.lock) junto da chart fina; o Chart.yaml declara
# a dependency com `repository: file://.../AksHelmBaseCharts/...` e o helm resolve do .tgz.
_VENDOR_DIR: Path = _TEMPLATES_DIR / "helm" / "basechart" / "_vendor"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(disabled_extensions=("j2", "yaml", "yml", "tpl")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _render_template(relpath: str, context: dict[str, object]) -> str:
    env = _jinja_env()
    template = env.get_template(relpath)
    return template.render(**context)


def _walk_static_helm_files(base_relpath: str) -> list[tuple[str, str]]:
    """Retorna pares (path_relativo_dentro_de_base, conteudo) para arquivos estaticos."""
    base = _TEMPLATES_DIR / Path(*base_relpath.split("/"))
    if not base.exists():
        return []
    out: list[tuple[str, str]] = []
    for child in sorted(base.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(base).as_posix()
        out.append((rel, child.read_text(encoding="utf-8")))
    return out


def strip_namespace_prefix(application_name: str, namespace: str) -> str:
    """Remove o prefixo '<namespace>-' do applicationName, se presente.

    Evita duplicar o namespace no serviceAccount (sa-<ns>-<applicationName>) e afins.
    Ex.: namespace='autorizador', application_name='autorizador-api-teste' -> 'api-teste'.
    Nao remove quando o nome e exatamente igual ao namespace (nao ha sufixo apos o hifen).
    """
    prefix = f"{namespace}-"
    if namespace and application_name.startswith(prefix) and len(application_name) > len(prefix):
        return application_name[len(prefix) :]
    return application_name


def default_apim_path(application_name: str) -> str:
    """pathApiManagementService: convencao do time (applicationName sem o prefixo 'api-')."""
    return application_name.removeprefix("api-")


def default_ingress_host(apim_path: str) -> str:
    """Host base do ingress: convencao 'services<path>' (ex.: utils -> servicesutils)."""
    return f"services{apim_path}"


# ASPNETCORE_ENVIRONMENT por ambiente (convencao do time). Fallback "Development" para
# ambientes fora do conjunto conhecido (DEVOPS_ENVIRONMENTS pode ter nomes custom).
_ASPNETCORE_ENVIRONMENT: dict[str, str] = {
    "dev": "Development",
    "qa": "Development",
    "uat": "Staging",
    "prod": "Production",
}


def aspnetcore_environment(env: str) -> str:
    return _ASPNETCORE_ENVIRONMENT.get(env, "Development")


# Porta HTTP do container por linguagem, para api. O modelo python do time sobe o
# uvicorn em 8000; os demais stacks seguem 8080 (ASPNETCORE_URLS no dotnet). A porta
# atravessa Dockerfile (EXPOSE/CMD), service e os ingresses - tem que casar nos tres.
_API_PORT_BY_LANGUAGE: dict[Language, int] = {Language.PYTHON: 8000}


def container_port(app_type: AppType, language: Language) -> int:
    default = _TYPE_DEFAULTS[app_type].container_port
    if app_type != AppType.API:
        return default
    return _API_PORT_BY_LANGUAGE.get(language, default)


def default_python_entrypoint(app_type: AppType) -> str:
    """Comando de entrada default da imagem Python (vira o CMD em exec form).

    Palpite razoavel por tipo - o usuario confirma/ajusta na pergunta do fluxo, ja
    que nao ha como inferir o modulo real da aplicacao sem clonar o repo.
    """
    if app_type == AppType.API:
        porta = container_port(AppType.API, Language.PYTHON)
        return f"uvicorn main:app --host 0.0.0.0 --port {porta}"
    return "python main.py"


def python_runtime_env(app_type: AppType, language: Language) -> dict[str, str]:
    """Variaveis de runtime Python emitidas no override por ambiente.

    Só o que descreve o AMBIENTE. As que descrevem a imagem (`PYTHONUNBUFFERED`,
    `PYTHONDONTWRITEBYTECODE`, `PYTHONPATH`) ficam no Dockerfile, como no modelo do
    time. `web` fica de fora (SPA estatica em nginx).
    """
    if language != Language.PYTHON or app_type == AppType.WEB:
        return {}
    return {"TZ": "America/Sao_Paulo"}


def dotnet_runtime_env(app_type: AppType, language: Language) -> dict[str, str]:
    """Variaveis de runtime .NET que antes eram fixadas no Dockerfile.

    O Dockerfile mantem apenas `DOTNET_RUNNING_IN_CONTAINER` (descreve a imagem, nao o
    ambiente); estas vivem no override por ambiente para serem ajustaveis sem rebuild.
    `web` fica de fora: e sempre uma SPA estatica servida por nginx.
    """
    if language != Language.DOTNET or app_type == AppType.WEB:
        return {}
    values = {"TZ": "America/Sao_Paulo"}
    if app_type == AppType.API:
        values["ASPNETCORE_URLS"] = "http://+:8080"
    values["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "false"
    values["Logging__LogLevel__Default"] = "Warning"
    values["Logging__LogLevel__Microsoft"] = "Warning"
    values["Logging__LogLevel__Microsoft.Hosting.Lifetime"] = "Information"
    return values


# Unified service tagging do Datadog: so os ambientes com agente coletando.
_DATADOG_ENVIRONMENTS = frozenset({"uat", "prod"})
_DATADOG_VERSION = "1.0"


def datadog_service(namespace: str, application_name: str, env: str) -> str:
    """Nome do servico no Datadog (ex.: autorizador-business-rules-api-k8s-uat)."""
    return f"{namespace}-{application_name}-k8s-{env}"


def datadog_tags(namespace: str, application_name: str, env: str) -> dict[str, str]:
    """Tags do unified service tagging (env/service/version), vazio fora de uat/prod."""
    if env not in _DATADOG_ENVIRONMENTS:
        return {}
    return {
        "env": env,
        "service": datadog_service(namespace, application_name, env),
        "version": _DATADOG_VERSION,
    }


def application_env(
    *,
    app_type: AppType,
    language: Language,
    namespace: str,
    application_name: str,
    env: str,
) -> dict[str, str]:
    """Bloco `env` do override por ambiente, montado a partir de tres fontes.

    Nesta ordem: defaults por ambiente (ASPNETCORE_ENVIRONMENT/timezone/Swagger), as
    variaveis de runtime que sairam do Dockerfile (ver `dotnet_runtime_env`) e o unified
    service tagging do Datadog (so uat/prod).
    """
    values: dict[str, str] = {}
    # Defaults do runtime .NET. `web` entra mesmo em outra linguagem: e sempre uma SPA
    # servida por nginx e o override e o mesmo do modelo do time.
    if app_type == AppType.WEB or (app_type == AppType.API and language == Language.DOTNET):
        values["ASPNETCORE_ENVIRONMENT"] = aspnetcore_environment(env)
        values["WEBSITE_TIME_ZONE"] = "America/Sao_Paulo"
    if app_type == AppType.API and language == Language.DOTNET:
        values["SwaggerEnabled"] = "true"
    values.update(dotnet_runtime_env(app_type, language))
    values.update(python_runtime_env(app_type, language))
    tags = datadog_tags(namespace, application_name, env)
    if tags:
        values["DD_ENV"] = tags["env"]
        values["DD_SERVICE"] = tags["service"]
        values["DD_VERSION"] = tags["version"]
    return values


def _ingress_hosts(host_base: str, env: str) -> dict[str, object]:
    """Deriva os hosts/secrets dos dois ingresses (primary + legacy) por ambiente.

    Convencao epharma: prod omite o segmento de ambiente; os demais o incluem.
    """
    if env == "prod":
        primary_host = f"{host_base}.k8s.epharma.com.br"
        primary_secret = "cert-wildcard-k8s-epharma-com-br-tls"
        legacy_host = f"{host_base}.epharma.com.br"
    else:
        primary_host = f"{host_base}.k8s.{env}.epharma.com.br"
        primary_secret = f"cert-wildcard-k8s-{env}-epharma-com-br-tls"
        legacy_host = f"{host_base}{env}.epharma.com.br"
    return {
        "ingress_primary_host": primary_host,
        "ingress_primary_secret": primary_secret,
        "ingress_legacy_host": legacy_host,
        "ingress_legacy_secret": "cert-wildcard-epharma-com-br-tls",
    }


def _build_template_context(
    *,
    app_name: str,
    app_type: AppType,
    language: Language,
    namespace: str,
    branch: str,
    acr_name: str,
    devops_templates_repo_name: str,
    devops_templates_tag: str,
    devops_templates_branch: str,
    schedule: str,
    pipeline_pass_language: bool,
    pipeline_template_path: str,
    dotnet_version: str,
    dotnet_entrypoint: str,
    python_version: str,
    python_package_manager: str,
    python_entrypoint: str,
    application_name: str,
    apim_path: str,
    api_ids: dict[str, str],
    ingress_host: str,
) -> dict[str, object]:
    defaults = _TYPE_DEFAULTS[app_type]
    entrypoint_argv = (python_entrypoint or default_python_entrypoint(app_type)).split()
    return {
        "app_name": app_name,
        # applicationName/pathApiManagementService/apiIds/host do ingress: perguntados no
        # fluxo, com defaults derivados quando aplicavel (ver executor/state_machine).
        # Os apiIds (template-aks) sao obrigatorios para api, sem default.
        "application_name": application_name,
        "apim_path": apim_path,
        # Normalizado com as quatro chaves para o template nao depender de `default`.
        "api_ids": {env: api_ids.get(env, "") for env in APIM_ENVIRONMENTS},
        # apiId escalar: o template-aks-python declara um unico parametro `apiId`.
        # A pergunta do fluxo restringe o stack python a um valor unico (single=True),
        # entao as quatro chaves de api_ids sao iguais e qualquer uma serve.
        "api_id": api_ids.get(APIM_ENVIRONMENTS[0], ""),
        "ingress_host": ingress_host,
        "app_type": app_type.value,
        "language": language.value,
        "schedule": schedule,
        "pipeline_pass_language": pipeline_pass_language,
        "pipeline_template_path": pipeline_template_path,
        "dotnet_version": dotnet_version,
        # tag da base image .NET (ex.: 8.0) a partir da versao informada (8.0.x).
        "dotnet_image_tag": dotnet_version.split(".")[0] + ".0",
        # assembly de entrada; default app_name.dll quando nao informado.
        "dotnet_entrypoint": dotnet_entrypoint or f"{app_name}.dll",
        "python_version": python_version,
        "python_package_manager": python_package_manager,
        # projectType do template-aks-python so aceita api|worker; cronjob deploya como worker
        # (o kind CronJob vem do nosso chart self-contained, nao do parametro).
        "python_project_type": "api" if app_type == AppType.API else "worker",
        # CMD em exec form: json.dumps evita depender do filtro `tojson` do Jinja, que
        # escapa <, > e & e mangleria argumentos.
        "python_entrypoint_cmd": json.dumps(entrypoint_argv),
        "namespace": namespace,
        "branch": branch,
        "acr_name": acr_name,
        "devops_templates_repo_name": devops_templates_repo_name,
        "devops_templates_tag": devops_templates_tag,
        "devops_templates_branch": devops_templates_branch,
        "container_port": container_port(app_type, language),
        "service_port": defaults.service_port,
        "health_path": defaults.health_path,
        "replica_count": defaults.replica_count,
        "resources_requests_cpu": defaults.requests_cpu,
        "resources_requests_memory": defaults.requests_memory,
        "resources_limits_cpu": defaults.limits_cpu,
        "resources_limits_memory": defaults.limits_memory,
    }


def render_dockerfile(
    app_type: AppType,
    language: Language,
    context: dict[str, object],
) -> str:
    # web e sempre uma SPA estatica servida por nginx (build Node), independente
    # da linguagem detectada. Os demais tipos escolhem o template pela linguagem.
    if app_type == AppType.WEB:
        return _render_template("dockerfile/web.Dockerfile.j2", context)
    return _render_template(f"dockerfile/{language.value}.Dockerfile.j2", context)


def _pipeline_stack(app_type: AppType, language: Language) -> str:
    """Stack que define o template de pipeline local (cada template do time tem contrato
    proprio). dotnet e python tem contrato real; node/go e web (sempre SPA em nginx)
    usam o generico _default ate termos os reais."""
    if app_type == AppType.WEB:
        return "_default"
    if language in (Language.DOTNET, Language.PYTHON):
        return language.value
    return "_default"


def render_pipeline_yaml(context: dict[str, object], *, stack: str = "_default") -> str:
    return _render_template(f"pipeline/{stack}/azure-pipelines.yaml.j2", context)


# Base charts do time (em AksHelmBaseCharts) por app_type: (nome, versao, kind).
# cronjob nao tem base chart -> chart self-contained (templates proprios).
_BASE_CHARTS: dict[AppType, tuple[str, str, str]] = {
    AppType.API: ("microservice-chart", "0.4.5", "microservice"),
    AppType.WEB: ("microservice-chart", "0.4.5", "microservice"),
    AppType.WORKER: ("worker-chart", "1.0.0", "worker"),
}


# Raiz da chart e dos overrides por ambiente DENTRO do repo da aplicacao.
_CHART_ROOT = "helm/base"
_ENVIRONMENTS_ROOT = "helm/environments"


def render_helm_chart(
    app_type: AppType,
    context: dict[str, object],
    environments: list[str],
) -> list[ScaffoldedFile]:
    """Renderiza o Helm chart (helm/base) + override por ambiente.

    api/web/worker geram uma chart fina que DEPENDE de uma base chart do time
    (microservice-chart/worker-chart), empacotada em helm/base/charts/<base>-<ver>.tgz
    com o Chart.lock correspondente, e values aninhados sob a chave da base chart.
    cronjob (sem base chart) mantem o chart self-contained com templates.
    """
    base = _BASE_CHARTS.get(app_type)
    if base is None:
        return _render_selfcontained_chart(app_type, context, environments)
    return _render_basechart_chart(app_type, base, context, environments)


def _env_context(app_type: AppType, context: dict[str, object], env: str) -> dict[str, object]:
    """Parte do contexto que muda por ambiente: bloco `env` da aplicacao + tags do Datadog."""
    namespace = str(context["namespace"])
    application_name = str(context["application_name"])
    return {
        "env": env,
        "app_env": application_env(
            app_type=app_type,
            language=Language(str(context["language"])),
            namespace=namespace,
            application_name=application_name,
            env=env,
        ),
        "datadog": datadog_tags(namespace, application_name, env),
    }


def _render_basechart_chart(
    app_type: AppType,
    base: tuple[str, str, str],
    context: dict[str, object],
    environments: list[str],
) -> list[ScaffoldedFile]:
    name, version, kind = base
    ctx = {**context, "base_chart_name": name, "base_chart_version": version}

    tgz_name = f"{name}-{version}.tgz"
    tgz_bytes = (_VENDOR_DIR / tgz_name).read_bytes()
    lock_text = (_VENDOR_DIR / f"{name}-{version}.lock").read_text(encoding="utf-8")

    files: list[ScaffoldedFile] = [
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/Chart.yaml",
            content=_render_template("helm/basechart/Chart.yaml.j2", ctx),
        ),
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/values.yaml",
            content=_render_template(f"helm/basechart/{kind}/values.yaml.j2", ctx),
        ),
        # Chart.lock + charts/<base>-<ver>.tgz vendorizados (par consistente do repo
        # modelo): o digest do lock corresponde ao bloco de dependencies do Chart.yaml.
        ScaffoldedFile(path=f"{_CHART_ROOT}/Chart.lock", content=lock_text),
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/charts/{tgz_name}",
            binary_b64=base64.b64encode(tgz_bytes).decode("ascii"),
        ),
    ]

    host_base = str(ctx["ingress_host"])
    for env in environments:
        # Todos os ambientes (incl. dev) recebem o skeleton completo. A referencia deixa
        # dev vazio, mas o agente gera o override para o usuario preencher/ajustar.
        env_ctx = {
            **ctx,
            **_env_context(app_type, ctx, env),
            **_ingress_hosts(host_base, env),
        }
        env_values = _render_template(f"environments/{kind}/values.yaml.j2", env_ctx)
        files.append(
            ScaffoldedFile(path=f"{_ENVIRONMENTS_ROOT}/{env}/values.yaml", content=env_values)
        )
    files.sort(key=lambda f: f.path)
    return files


def _render_selfcontained_chart(
    app_type: AppType,
    context: dict[str, object],
    environments: list[str],
) -> list[ScaffoldedFile]:
    """Chart self-contained (cronjob): Chart.yaml + values + templates proprios."""
    files: list[ScaffoldedFile] = []

    files.append(
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/Chart.yaml",
            content=_render_template("helm/_common/Chart.yaml.j2", context),
        )
    )
    files.append(
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/values.yaml",
            content=_render_template("helm/_common/values.yaml.j2", context),
        )
    )
    for rel, content in _walk_static_helm_files("helm/_common/templates"):
        files.append(ScaffoldedFile(path=f"{_CHART_ROOT}/templates/{rel}", content=content))
    for rel, content in _walk_static_helm_files(f"helm/{app_type.value}/templates"):
        files.append(ScaffoldedFile(path=f"{_CHART_ROOT}/templates/{rel}", content=content))

    for env in environments:
        env_values = _render_template(
            "environments/values.yaml.j2", {**context, **_env_context(app_type, context, env)}
        )
        files.append(
            ScaffoldedFile(path=f"{_ENVIRONMENTS_ROOT}/{env}/values.yaml", content=env_values)
        )

    files.sort(key=lambda f: f.path)
    return files


def build_bundle(
    *,
    app_name: str,
    app_type: AppType,
    namespace: str,
    branch: str,
    acr_name: str,
    devops_templates_repo_name: str,
    devops_templates_tag: str,
    devops_templates_branch: str = DEFAULT_TEMPLATES_BRANCH,
    language: Language = Language.DOTNET,
    schedule: str = DEFAULT_CRON_SCHEDULE,
    pipeline_pass_language: bool = False,
    pipeline_template_path: str = DEFAULT_PIPELINE_TEMPLATE_PATH,
    dotnet_version: str = DEFAULT_DOTNET_VERSION,
    dotnet_entrypoint: str = "",
    python_version: str = DEFAULT_PYTHON_VERSION,
    python_package_manager: str = DEFAULT_PYTHON_PACKAGE_MANAGER,
    python_entrypoint: str = "",
    application_name: str = "",
    apim_path: str = "",
    api_ids: dict[str, str] | None = None,
    ingress_host: str = "",
    environments: list[str] | None = None,
) -> ScaffoldBundle:
    """Renderiza todos os artefatos em memoria."""
    envs = environments if environments is not None else list(DEFAULT_ENVIRONMENTS)
    # Defaults derivados quando o usuario nao informa (mesma convencao do state_machine).
    # Remove o prefixo do namespace do applicationName (idempotente com o state_machine).
    resolved_app = strip_namespace_prefix(application_name or app_name, namespace)
    resolved_path = apim_path or default_apim_path(resolved_app)
    resolved_host = ingress_host or default_ingress_host(resolved_path)
    ctx = _build_template_context(
        app_name=app_name,
        app_type=app_type,
        language=language,
        namespace=namespace,
        branch=branch,
        acr_name=acr_name,
        devops_templates_repo_name=devops_templates_repo_name,
        devops_templates_tag=devops_templates_tag,
        devops_templates_branch=devops_templates_branch,
        schedule=schedule,
        pipeline_pass_language=pipeline_pass_language,
        pipeline_template_path=pipeline_template_path,
        dotnet_version=dotnet_version,
        dotnet_entrypoint=dotnet_entrypoint,
        python_version=python_version,
        python_package_manager=python_package_manager,
        python_entrypoint=python_entrypoint,
        application_name=resolved_app,
        apim_path=resolved_path,
        api_ids=api_ids or {},
        ingress_host=resolved_host,
    )

    files = [
        ScaffoldedFile(path="Dockerfile", content=render_dockerfile(app_type, language, ctx)),
        ScaffoldedFile(
            path="azure-pipelines.yaml",
            content=render_pipeline_yaml(ctx, stack=_pipeline_stack(app_type, language)),
        ),
        *render_helm_chart(app_type, ctx, envs),
    ]

    return ScaffoldBundle(files=files)


def dry_run(
    *,
    app_name: str,
    app_type: AppType,
    namespace: str,
    branch: str,
    acr_name: str,
    devops_templates_repo_name: str,
    devops_templates_tag: str,
    devops_templates_branch: str = DEFAULT_TEMPLATES_BRANCH,
    language: Language = Language.DOTNET,
    schedule: str = DEFAULT_CRON_SCHEDULE,
    pipeline_pass_language: bool = False,
    pipeline_template_path: str = DEFAULT_PIPELINE_TEMPLATE_PATH,
    dotnet_version: str = DEFAULT_DOTNET_VERSION,
    dotnet_entrypoint: str = "",
    python_version: str = DEFAULT_PYTHON_VERSION,
    python_package_manager: str = DEFAULT_PYTHON_PACKAGE_MANAGER,
    python_entrypoint: str = "",
    application_name: str = "",
    apim_path: str = "",
    api_ids: dict[str, str] | None = None,
    ingress_host: str = "",
    environments: list[str] | None = None,
    existing_repo_paths: set[str] | None = None,
) -> ToolResult[DryRunResult]:
    """Renderiza todos os arquivos em memoria e checa colisoes contra arquivos existentes."""
    try:
        bundle = build_bundle(
            app_name=app_name,
            app_type=app_type,
            language=language,
            namespace=namespace,
            branch=branch,
            acr_name=acr_name,
            devops_templates_repo_name=devops_templates_repo_name,
            devops_templates_tag=devops_templates_tag,
            devops_templates_branch=devops_templates_branch,
            schedule=schedule,
            pipeline_pass_language=pipeline_pass_language,
            pipeline_template_path=pipeline_template_path,
            dotnet_version=dotnet_version,
            dotnet_entrypoint=dotnet_entrypoint,
            python_version=python_version,
            python_package_manager=python_package_manager,
            python_entrypoint=python_entrypoint,
            application_name=application_name,
            apim_path=apim_path,
            api_ids=api_ids or {},
            ingress_host=ingress_host,
            environments=environments,
        )
    except Exception as exc:  # noqa: BLE001 - tools nunca propagam excecao
        return ToolResult.failure(
            f"Falha renderizando templates: {exc}",
            code="scaffold.render_error",
        )

    existing = {_normalize(e) for e in (existing_repo_paths or set())}
    collisions = sorted(p for p in bundle.all_paths if _normalize(p) in existing)

    return ToolResult.success(DryRunResult(bundle=bundle, collisions=collisions))


def _normalize(path: str) -> str:
    return str(PurePosixPath(path.lstrip("/")))
