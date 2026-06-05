"""Tool de scaffold: renderiza templates Jinja2 e monta o bundle de arquivos.

Esta tool e a parte DETERMINISTICA da geracao - o LLM nao escreve YAML ou Dockerfile.
Ele apenas decide *quando* chamar render/dry_run e com quais parametros tipados.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from devops_agent.models import AppType, ToolResult


class ScaffoldedFile(BaseModel):
    """Um arquivo a ser escrito em um repositorio."""

    path: str
    content: str


class ScaffoldBundle(BaseModel):
    """Conjunto completo de arquivos resultantes do scaffold."""

    app_repo_files: list[ScaffoldedFile]
    helm_chart_files: list[ScaffoldedFile]

    @property
    def all_app_paths(self) -> list[str]:
        return [f.path for f in self.app_repo_files]

    @property
    def all_helm_paths(self) -> list[str]:
        return [f.path for f in self.helm_chart_files]


class DryRunResult(BaseModel):
    """Resultado da pre-renderizacao com colisoes detectadas."""

    bundle: ScaffoldBundle
    app_repo_collisions: list[str] = []
    helm_repo_collisions: list[str] = []

    @property
    def has_collisions(self) -> bool:
        return bool(self.app_repo_collisions or self.helm_repo_collisions)


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
        health_path="/healthz",
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
}


_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"


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
    namespace: str,
    branch: str,
    acr_name: str,
    devops_templates_repo_name: str,
    devops_templates_tag: str,
) -> dict[str, object]:
    defaults = _TYPE_DEFAULTS[app_type]
    return {
        "app_name": app_name,
        "app_type": app_type.value,
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


def render_dockerfile(app_type: AppType, context: dict[str, object]) -> str:
    return _render_template(f"dockerfile/{app_type.value}.Dockerfile.j2", context)


def render_pipeline_yaml(context: dict[str, object]) -> str:
    return _render_template("pipeline/azure-pipelines.yaml.j2", context)


def render_helm_chart(
    app_type: AppType,
    context: dict[str, object],
) -> list[ScaffoldedFile]:
    """Renderiza o Helm chart completo combinando _common + tipo-especifico."""
    files: list[ScaffoldedFile] = []
    app_name = context["app_name"]

    chart_yaml = _render_template("helm/_common/Chart.yaml.j2", context)
    values_yaml = _render_template("helm/_common/values.yaml.j2", context)

    chart_root = f"charts/{app_name}"
    files.append(ScaffoldedFile(path=f"{chart_root}/Chart.yaml", content=chart_yaml))
    files.append(ScaffoldedFile(path=f"{chart_root}/values.yaml", content=values_yaml))

    # Templates Helm estaticos vindos de _common/templates/
    for rel, content in _walk_static_helm_files("helm/_common/templates"):
        files.append(ScaffoldedFile(path=f"{chart_root}/templates/{rel}", content=content))

    # Templates Helm especificos do tipo (api/worker/web)
    for rel, content in _walk_static_helm_files(f"helm/{app_type.value}/templates"):
        files.append(ScaffoldedFile(path=f"{chart_root}/templates/{rel}", content=content))

    # Ordena para determinismo (testes de snapshot)
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
) -> ScaffoldBundle:
    """Renderiza todos os artefatos em memoria."""
    ctx = _build_template_context(
        app_name=app_name,
        app_type=app_type,
        namespace=namespace,
        branch=branch,
        acr_name=acr_name,
        devops_templates_repo_name=devops_templates_repo_name,
        devops_templates_tag=devops_templates_tag,
    )

    app_files = [
        ScaffoldedFile(path="Dockerfile", content=render_dockerfile(app_type, ctx)),
        ScaffoldedFile(
            path="azure-pipelines.yaml",
            content=render_pipeline_yaml(ctx),
        ),
    ]
    helm_files = render_helm_chart(app_type, ctx)

    return ScaffoldBundle(app_repo_files=app_files, helm_chart_files=helm_files)


def dry_run(
    *,
    app_name: str,
    app_type: AppType,
    namespace: str,
    branch: str,
    acr_name: str,
    devops_templates_repo_name: str,
    devops_templates_tag: str,
    existing_app_repo_paths: set[str] | None = None,
    existing_helm_repo_paths: set[str] | None = None,
) -> ToolResult[DryRunResult]:
    """Renderiza todos os arquivos em memoria e checa colisoes contra arquivos existentes."""
    try:
        bundle = build_bundle(
            app_name=app_name,
            app_type=app_type,
            namespace=namespace,
            branch=branch,
            acr_name=acr_name,
            devops_templates_repo_name=devops_templates_repo_name,
            devops_templates_tag=devops_templates_tag,
        )
    except Exception as exc:  # noqa: BLE001 - tools nunca propagam excecao
        return ToolResult.failure(
            f"Falha renderizando templates: {exc}",
            code="scaffold.render_error",
        )

    app_existing = existing_app_repo_paths or set()
    helm_existing = existing_helm_repo_paths or set()

    app_collisions = sorted(
        p for p in bundle.all_app_paths if _normalize(p) in {_normalize(e) for e in app_existing}
    )
    helm_collisions = sorted(
        p for p in bundle.all_helm_paths if _normalize(p) in {_normalize(e) for e in helm_existing}
    )

    return ToolResult.success(
        DryRunResult(
            bundle=bundle,
            app_repo_collisions=app_collisions,
            helm_repo_collisions=helm_collisions,
        )
    )


def _normalize(path: str) -> str:
    return str(PurePosixPath(path.lstrip("/")))
