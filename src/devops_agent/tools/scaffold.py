"""Tool de scaffold: renderiza templates Jinja2 e monta o bundle de arquivos.

Esta tool e a parte DETERMINISTICA da geracao - o LLM nao escreve YAML ou Dockerfile.
Ele apenas decide *quando* chamar render/dry_run e com quais parametros tipados.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from devops_agent.models import AppType, Language, ToolResult


class ScaffoldedFile(BaseModel):
    """Um arquivo de texto a ser escrito no repositorio da aplicacao."""

    path: str
    content: str = ""


class ScaffoldBundle(BaseModel):
    """Conjunto completo de arquivos resultantes do scaffold.

    Tudo vai para o repositorio da PROPRIA aplicacao (Dockerfile,
    azure-pipelines.yaml e o Helm em helm/manifests + helm/environments).
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

# Versao default do .NET quando o usuario nao informa.
DEFAULT_DOTNET_VERSION = "8.0.x"

# Ambientes default para os quais gerar override de values.
DEFAULT_ENVIRONMENTS = ("dev", "qa", "uat", "prod")

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"

# Base charts vendorizadas DESCOMPACTADAS (deployment/service/ingress/etc.),
# copiadas verbatim do repo modelo do time (AksHelmCharts). O agente as emite como
# arquivos puros em helm/manifests/charts/<base>/ junto com a chart fina, entao o
# `helm` resolve a dependencia do diretorio sem `helm dependency build` nem `.tgz`.
_VENDOR_RELPATH = "helm/basechart/_vendor"


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
    schedule: str,
    pipeline_pass_language: bool,
    pipeline_template_path: str,
    dotnet_version: str,
    dotnet_entrypoint: str,
) -> dict[str, object]:
    defaults = _TYPE_DEFAULTS[app_type]
    return {
        "app_name": app_name,
        # path de APIM/automation: convencao do time (app_name sem o prefixo 'api-').
        "app_path": app_name.removeprefix("api-"),
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
        "namespace": namespace,
        "branch": branch,
        "acr_name": acr_name,
        "devops_templates_repo_name": devops_templates_repo_name,
        "devops_templates_tag": devops_templates_tag,
        "container_port": defaults.container_port,
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
    """Stack que define o template de pipeline local. So dotnet tem contrato real;
    os demais (e web) usam o template generico _default ate termos os reais."""
    if app_type != AppType.WEB and language == Language.DOTNET:
        return "dotnet"
    return "_default"


def render_pipeline_yaml(context: dict[str, object], *, stack: str = "_default") -> str:
    return _render_template(f"pipeline/{stack}/azure-pipelines.yaml.j2", context)


# Base charts do time (em AksHelmBaseCharts) por app_type: (nome, versao, kind).
# cronjob nao tem base chart -> chart self-contained (templates proprios).
_BASE_CHARTS: dict[AppType, tuple[str, str, str]] = {
    AppType.API: ("microservice-chart", "0.4.4", "microservice"),
    AppType.WEB: ("microservice-chart", "0.4.4", "microservice"),
    AppType.WORKER: ("worker-chart", "1.0.0", "worker"),
}


# Raiz da chart e dos overrides por ambiente DENTRO do repo da aplicacao.
_CHART_ROOT = "helm/manifests"
_ENVIRONMENTS_ROOT = "helm/environments"


def render_helm_chart(
    app_type: AppType,
    context: dict[str, object],
    environments: list[str],
) -> list[ScaffoldedFile]:
    """Renderiza o Helm chart (helm/manifests) + override por ambiente.

    api/web/worker geram uma chart fina que DEPENDE de uma base chart do time
    (microservice-chart/worker-chart) vendorizada descompactada em
    helm/manifests/charts/<base>/, com values aninhados sob a chave da base
    chart. cronjob (sem base chart) mantem o chart self-contained com templates.
    """
    base = _BASE_CHARTS.get(app_type)
    if base is None:
        return _render_selfcontained_chart(app_type, context, environments)
    return _render_basechart_chart(base, context, environments)


def _render_basechart_chart(
    base: tuple[str, str, str],
    context: dict[str, object],
    environments: list[str],
) -> list[ScaffoldedFile]:
    name, version, kind = base
    ctx = {**context, "base_chart_name": name, "base_chart_version": version}

    files: list[ScaffoldedFile] = [
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/Chart.yaml",
            content=_render_template("helm/basechart/Chart.yaml.j2", ctx),
        ),
        ScaffoldedFile(
            path=f"{_CHART_ROOT}/values.yaml",
            content=_render_template(f"helm/basechart/{kind}/values.yaml.j2", ctx),
        ),
    ]
    # Base chart vendorizada descompactada: o helm resolve a dependency declarada
    # no Chart.yaml direto do diretorio charts/<base>/, sem Chart.lock nem .tgz.
    for rel, content in _walk_static_helm_files(f"{_VENDOR_RELPATH}/{name}"):
        files.append(ScaffoldedFile(path=f"{_CHART_ROOT}/charts/{name}/{rel}", content=content))

    for env in environments:
        env_values = _render_template(f"environments/{kind}/values.yaml.j2", {**ctx, "env": env})
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
        env_values = _render_template("environments/values.yaml.j2", {**context, "env": env})
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
    language: Language = Language.DOTNET,
    schedule: str = DEFAULT_CRON_SCHEDULE,
    pipeline_pass_language: bool = False,
    pipeline_template_path: str = DEFAULT_PIPELINE_TEMPLATE_PATH,
    dotnet_version: str = DEFAULT_DOTNET_VERSION,
    dotnet_entrypoint: str = "",
    environments: list[str] | None = None,
) -> ScaffoldBundle:
    """Renderiza todos os artefatos em memoria."""
    envs = environments if environments is not None else list(DEFAULT_ENVIRONMENTS)
    ctx = _build_template_context(
        app_name=app_name,
        app_type=app_type,
        language=language,
        namespace=namespace,
        branch=branch,
        acr_name=acr_name,
        devops_templates_repo_name=devops_templates_repo_name,
        devops_templates_tag=devops_templates_tag,
        schedule=schedule,
        pipeline_pass_language=pipeline_pass_language,
        pipeline_template_path=pipeline_template_path,
        dotnet_version=dotnet_version,
        dotnet_entrypoint=dotnet_entrypoint,
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
    language: Language = Language.DOTNET,
    schedule: str = DEFAULT_CRON_SCHEDULE,
    pipeline_pass_language: bool = False,
    pipeline_template_path: str = DEFAULT_PIPELINE_TEMPLATE_PATH,
    dotnet_version: str = DEFAULT_DOTNET_VERSION,
    dotnet_entrypoint: str = "",
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
            schedule=schedule,
            pipeline_pass_language=pipeline_pass_language,
            pipeline_template_path=pipeline_template_path,
            dotnet_version=dotnet_version,
            dotnet_entrypoint=dotnet_entrypoint,
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
