"""Executor da fase EXECUTE: orquestra clone, write, commit, push e PR
nos dois repositorios (app e devops-templates).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from devops_agent.config import Settings
from devops_agent.models import ExecutionResult, SessionState, ToolResult
from devops_agent.tools.azdo import AzdoClient
from devops_agent.tools.git_ops import (
    GitAuth,
    clone_shallow,
    create_branch_and_commit,
    list_existing_paths,
    push_branch,
    temp_workspace,
    write_files,
)
from devops_agent.tools.scaffold import ScaffoldBundle, build_bundle, dry_run

logger = logging.getLogger(__name__)


@dataclass
class DevOpsTemplatesRepo:
    """Coordenadas do repo do time de DevOps."""

    clone_url: str
    project: str
    repository: str
    tag: str


def _parse_devops_repo(settings: Settings) -> DevOpsTemplatesRepo:
    """Extrai project/repo da URL do devops-templates."""
    from devops_agent.tools.validation import validate_repo_url

    r = validate_repo_url(settings.devops_templates_repo_url, allowed_org=settings.azdo_org)
    if not r.ok or r.data is None:
        raise ValueError(
            f"DEVOPS_TEMPLATES_REPO_URL invalido: {r.error}"
        )
    return DevOpsTemplatesRepo(
        clone_url=settings.devops_templates_repo_url,
        project=r.data.project,
        repository=r.data.repository,
        tag=settings.devops_templates_tag,
    )


def _git_auth_from_settings(settings: Settings) -> GitAuth | None:
    if settings.azdo_auth_mode == "pat" and settings.azdo_pat:
        return GitAuth(username="pat", password=settings.azdo_pat)
    return None


def render_summary(bundle: ScaffoldBundle) -> str:
    """Mensagem de confirmacao mostrada ao usuario antes do EXECUTE."""
    app_paths = "\n".join(f"  - {f.path}" for f in bundle.app_repo_files)
    helm_paths = "\n".join(f"  - {f.path}" for f in bundle.helm_chart_files)
    return (
        "Vou criar os seguintes arquivos:\n\n"
        "No repositorio da aplicacao:\n"
        f"{app_paths}\n\n"
        "No repositorio do time de DevOps (devops-templates):\n"
        f"{helm_paths}\n"
    )


def prepare_dry_run(
    session: SessionState,
    settings: Settings,
) -> ToolResult:
    """Executa dry_run combinando os inputs do usuario com configuracao."""
    if not session.repo_coords or not session.app_type or not session.app_name or not session.branch or not session.namespace:
        return ToolResult.failure("Sessao incompleta para dry_run.", code="session.incomplete")

    devops_repo = _parse_devops_repo(settings)
    acr_name = _derive_acr_name(settings)

    return dry_run(
        app_name=session.app_name,
        app_type=session.app_type,
        namespace=session.namespace,
        branch=session.branch,
        acr_name=acr_name,
        devops_templates_repo_name=f"{devops_repo.project}/{devops_repo.repository}",
        devops_templates_tag=devops_repo.tag,
    )


def _derive_acr_name(settings: Settings) -> str:
    """Convencao: {org}acr em lowercase. Podera virar setting dedicado."""
    return f"{settings.azdo_org.lower()}acr"


def _timestamp_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def execute(
    session: SessionState,
    settings: Settings,
    *,
    workdir_root: Path | None = None,
) -> ToolResult[ExecutionResult]:
    """Pipeline EXECUTE: clona app + devops repos, escreve, commita, faz push e abre PRs."""
    if not session.confirmed:
        return ToolResult.failure(
            "Usuario nao confirmou - executar abortado.", code="execute.not_confirmed"
        )

    bundle_result = prepare_dry_run(session, settings)
    if not bundle_result.ok:
        return ToolResult.failure(bundle_result.error or "dry_run falhou", code="execute.dry_run")

    dry = bundle_result.data
    assert dry is not None
    bundle = dry.bundle
    devops_repo = _parse_devops_repo(settings)
    git_auth = _git_auth_from_settings(settings)

    assert session.repo_coords is not None
    assert session.app_type is not None
    assert session.app_name is not None
    assert session.branch is not None

    app_branch = f"scaffold/{session.app_type.value}-{_timestamp_slug()}"
    helm_branch = f"scaffold/{session.app_name}-helm-{_timestamp_slug()}"
    commit_msg = (
        f"chore(ci): scaffold pipeline para {session.app_name} ({session.app_type.value}) "
        f"via agent (session={session.session_id})"
    )
    pr_description = (
        f"Pipeline gerada automaticamente pelo devops-pipeline-agent.\n\n"
        f"- Session: {session.session_id}\n"
        f"- Solicitante: {session.requested_by or 'desconhecido'}\n"
        f"- App: {session.app_name}\n"
        f"- Tipo: {session.app_type.value}\n"
        f"- Namespace: {session.namespace}\n"
    )

    execution = ExecutionResult()

    with temp_workspace(root=workdir_root) as ws:
        # 1) Repo da aplicacao
        app_clone = clone_shallow(
            ws,
            key="app",
            clone_url=session.repo_coords.to_clone_url(),
            branch=session.branch,
            auth=git_auth,
        )
        if not app_clone.ok or app_clone.data is None:
            return ToolResult.failure(app_clone.error or "clone app falhou", code="execute.app_clone")

        existing_app = list_existing_paths(app_clone.data)
        colisoes_app = [f.path for f in bundle.app_repo_files if f.path in existing_app]
        if colisoes_app:
            return ToolResult.failure(
                f"Arquivos ja existem no repo da app: {', '.join(colisoes_app)}",
                code="execute.app_collision",
            )

        w1 = write_files(app_clone.data, bundle.app_repo_files)
        if not w1.ok:
            return ToolResult.failure(w1.error or "write app falhou", code="execute.app_write")

        b1 = create_branch_and_commit(
            app_clone.data,
            new_branch=app_branch,
            commit_message=commit_msg,
        )
        if not b1.ok:
            return ToolResult.failure(b1.error or "commit app falhou", code="execute.app_commit")

        p1 = push_branch(app_clone.data, branch=app_branch, auth=git_auth)
        if not p1.ok:
            return ToolResult.failure(p1.error or "push app falhou", code="execute.app_push")

        execution.app_branch = app_branch

        # 2) Repo devops-templates
        helm_clone = clone_shallow(
            ws,
            key="devops",
            clone_url=devops_repo.clone_url,
            branch="main",  # convencao - a tag e referenciada no pipeline yaml
            auth=git_auth,
        )
        if not helm_clone.ok or helm_clone.data is None:
            return ToolResult.failure(
                helm_clone.error or "clone devops falhou", code="execute.devops_clone"
            )

        existing_devops = list_existing_paths(helm_clone.data)
        colisoes_devops = [f.path for f in bundle.helm_chart_files if f.path in existing_devops]
        if colisoes_devops:
            return ToolResult.failure(
                f"Arquivos ja existem no repo de devops-templates: {', '.join(colisoes_devops[:3])}",
                code="execute.devops_collision",
            )

        w2 = write_files(helm_clone.data, bundle.helm_chart_files)
        if not w2.ok:
            return ToolResult.failure(w2.error or "write devops falhou", code="execute.devops_write")

        b2 = create_branch_and_commit(
            helm_clone.data,
            new_branch=helm_branch,
            commit_message=commit_msg,
        )
        if not b2.ok:
            return ToolResult.failure(b2.error or "commit devops falhou", code="execute.devops_commit")

        p2 = push_branch(helm_clone.data, branch=helm_branch, auth=git_auth)
        if not p2.ok:
            return ToolResult.failure(p2.error or "push devops falhou", code="execute.devops_push")

        execution.helm_branch = helm_branch

    # 3) Abre PRs via Azure DevOps REST API (opcional - se PAT disponivel)
    if not settings.azdo_pat:
        logger.warning("AZDO_PAT nao configurado - PRs nao serao abertos automaticamente.")
        session.execution = execution
        return ToolResult.success(execution)

    with AzdoClient(organization=settings.azdo_org, pat=settings.azdo_pat) as client:
        pr_app = client.create_pull_request(
            session.repo_coords.project,
            session.repo_coords.repository,
            source_branch=app_branch,
            target_branch=session.branch,
            title=f"chore(ci): scaffold pipeline {session.app_name}",
            description=pr_description,
        )
        if pr_app.ok and pr_app.data is not None:
            execution.app_pr_url = pr_app.data.web_url

        reviewers = [r.strip() for r in settings.devops_templates_reviewers.split(",") if r.strip()]
        pr_helm = client.create_pull_request(
            devops_repo.project,
            devops_repo.repository,
            source_branch=helm_branch,
            target_branch="main",
            title=f"chore(charts): novo chart para {session.app_name}",
            description=pr_description,
            reviewers=reviewers or None,
        )
        if pr_helm.ok and pr_helm.data is not None:
            execution.helm_pr_url = pr_helm.data.web_url

    session.execution = execution
    return ToolResult.success(execution)
