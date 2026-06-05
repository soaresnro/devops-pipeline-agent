"""Executor da fase EXECUTE: orquestra clone, write, commit, push e PR
nos dois repositorios (app e devops-templates).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from devops_agent.config import Settings
from devops_agent.models import AppType, ExecutionResult, Language, Phase, SessionState, ToolResult
from devops_agent.tools.auth import acquire_app_token, acquire_user_obo_token
from devops_agent.tools.azdo import AzdoClient
from devops_agent.tools.detection import LanguageDetection, detect_language
from devops_agent.tools.git_ops import (
    GitAuth,
    clone_shallow,
    create_branch_and_commit,
    list_existing_paths,
    push_branch,
    temp_workspace,
    write_files,
)
from devops_agent.tools.scaffold import (
    DEFAULT_CRON_SCHEDULE,
    DryRunResult,
    ScaffoldBundle,
    build_bundle,
    dry_run,
)

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


def _git_auth_from_settings(settings: Settings, app_token: str | None = None) -> GitAuth | None:
    if settings.azdo_auth_mode == "sp":
        return GitAuth.bearer(app_token) if app_token else None
    if settings.azdo_pat:
        return GitAuth(username="pat", password=settings.azdo_pat)
    return None


def _check_user_permission(
    session: SessionState,
    settings: Settings,
    app_token: str,
) -> ToolResult[None]:
    """Verifica (fail-closed) que o usuario humano tem Contribute no repo, via OBO.

    Resolve os GUIDs com o app token (leitura de metadados) e avalia a permissao
    com o token OBO do usuario. Qualquer falha - sem assercao, OBO, API ou
    permissao negada - recusa sem prosseguir (o SP nunca vira privilege escalation).
    """
    assert session.repo_coords is not None
    if not session.user_assertion:
        return ToolResult.failure(
            "Token do usuario ausente - nao e possivel validar a permissao (OBO).",
            code="execute.missing_user_assertion",
        )

    with AzdoClient(organization=settings.azdo_org, bearer_token=app_token) as app_client:
        repo = app_client.get_repo(
            session.repo_coords.project, session.repo_coords.repository
        )
    if not repo.ok or repo.data is None or not repo.data.project_id:
        return ToolResult.failure(
            repo.error or "Nao foi possivel resolver o repositorio para o check de permissao.",
            code=repo.error_code or "execute.repo_resolve",
        )

    obo = acquire_user_obo_token(settings, session.user_assertion)
    if not obo.ok or obo.data is None:
        return ToolResult.failure(obo.error or "Falha OBO.", code=obo.error_code or "auth.obo")

    with AzdoClient(organization=settings.azdo_org, bearer_token=obo.data) as user_client:
        perm = user_client.check_user_permission(repo.data.project_id, repo.data.id)

    session.record_audit(
        "permission_checked",
        repo=session.repo_coords.full_name,
        oid=session.user_oid,
        allowed=bool(perm.ok and perm.data),
    )

    if not perm.ok:
        return ToolResult.failure(
            perm.error or "Falha consultando a permissao do usuario.",
            code=perm.error_code or "execute.permission_query",
        )
    if perm.data is not True:
        return ToolResult.failure(
            "Usuario nao tem permissao de Contribute neste repositorio.",
            code="execute.permission_denied",
        )
    return ToolResult.success(None)


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
) -> ToolResult[DryRunResult]:
    """Executa dry_run combinando os inputs do usuario com configuracao."""
    if not session.repo_coords or not session.app_type or not session.app_name or not session.branch or not session.namespace:
        return ToolResult.failure("Sessao incompleta para dry_run.", code="session.incomplete")

    devops_repo = _parse_devops_repo(settings)
    acr_name = _derive_acr_name(settings)

    language = session.language or Language.DOTNET
    return dry_run(
        app_name=session.app_name,
        app_type=session.app_type,
        language=language,
        schedule=session.schedule or DEFAULT_CRON_SCHEDULE,
        pipeline_pass_language=settings.pipeline_pass_language,
        pipeline_template_path=_pipeline_template_path(settings, session.app_type, language),
        namespace=session.namespace,
        branch=session.branch,
        acr_name=acr_name,
        devops_templates_repo_name=f"{devops_repo.project}/{devops_repo.repository}",
        devops_templates_tag=devops_repo.tag,
    )


def detect_language_step(
    session: SessionState,
    settings: Settings,
) -> ToolResult[LanguageDetection]:
    """Detecta a linguagem do repo (via Items API, sem clone) e transiciona a fase.

    Usa o app token do SP (modo sp) ou o PAT (modo pat) para chamar a Items API.
    Best-effort: ausencia de credencial, falta de pre-requisitos ou qualquer falha
    sao tratadas como inconclusivo -> ASK_LANGUAGE (nunca derruba a sessao). Sempre
    sai da fase DETECT_LANGUAGE para evitar loop no driver.
    """

    def _inconclusive(candidates: list[Language] | None = None) -> ToolResult[LanguageDetection]:
        session.language_candidates = candidates or []
        session.phase = Phase.ASK_LANGUAGE
        return ToolResult.success(
            LanguageDetection(language=None, candidates=candidates or [], is_conclusive=False)
        )

    if not session.repo_coords or not session.branch:
        return _inconclusive()

    # Cliente conforme o modo de auth: bearer (app token do SP) no modo sp, PAT no modo pat.
    if settings.azdo_auth_mode == "sp":
        token = acquire_app_token(settings)
        if not token.ok or token.data is None:
            return _inconclusive()
        client = AzdoClient(organization=settings.azdo_org, bearer_token=token.data)
    elif settings.azdo_pat:
        client = AzdoClient(organization=settings.azdo_org, pat=settings.azdo_pat)
    else:
        return _inconclusive()

    with client:
        items = client.list_root_items(
            session.repo_coords.project,
            session.repo_coords.repository,
            session.branch,
        )
    if not items.ok or items.data is None:
        return _inconclusive()

    detection = detect_language(items.data).data
    assert detection is not None  # detect_language sempre retorna sucesso
    if detection.is_conclusive and detection.language is not None:
        session.language = detection.language
        session.phase = Phase.VALIDATE
        return ToolResult.success(detection)

    return _inconclusive(detection.candidates)


def _derive_acr_name(settings: Settings) -> str:
    """Convencao: {org}acr em lowercase. Podera virar setting dedicado."""
    return f"{settings.azdo_org.lower()}acr"


def _pipeline_template_path(settings: Settings, app_type: AppType, language: Language) -> str:
    """Escolhe o template de pipeline pela linguagem; web reaproveita o de node.

    Cai no devops_template_default quando a linguagem nao tem caminho proprio.
    """
    key = Language.NODE if app_type == AppType.WEB else language
    by_language = {
        Language.DOTNET: settings.devops_template_dotnet,
        Language.NODE: settings.devops_template_node,
        Language.PYTHON: settings.devops_template_python,
        Language.GO: settings.devops_template_go,
    }
    return by_language.get(key) or settings.devops_template_default


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

    assert session.repo_coords is not None
    assert session.app_type is not None
    assert session.app_name is not None
    assert session.branch is not None

    # Modo sp: adquire o token do SP uma vez e valida a permissao do usuario humano
    # (OBO) ANTES de qualquer clone. Fail-closed: recusa sem usar o SP se reprovar.
    app_token: str | None = None
    if settings.azdo_auth_mode == "sp":
        token_result = acquire_app_token(settings)
        if not token_result.ok or token_result.data is None:
            return ToolResult.failure(
                token_result.error or "Falha adquirindo token do Service Principal.",
                code=token_result.error_code or "execute.app_token",
            )
        app_token = token_result.data

        perm_check = _check_user_permission(session, settings, app_token)
        if not perm_check.ok:
            return ToolResult.failure(
                perm_check.error or "Permissao do usuario negada.",
                code=perm_check.error_code or "execute.permission_denied",
            )

    git_auth = _git_auth_from_settings(settings, app_token)

    app_branch = f"scaffold/{session.app_type.value}-{_timestamp_slug()}"
    helm_branch = f"scaffold/{session.app_name}-helm-{_timestamp_slug()}"
    commit_msg = (
        f"chore(ci): scaffold pipeline para {session.app_name} ({session.app_type.value}) "
        f"via agent (session={session.session_id})"
    )
    oid_suffix = f" (oid={session.user_oid})" if session.user_oid else ""
    pr_description = (
        f"Pipeline gerada automaticamente pelo devops-pipeline-agent.\n\n"
        f"Generated by CI/CD Agent on behalf of: {session.requested_by or 'desconhecido'}"
        f"{oid_suffix} session={session.session_id}\n\n"
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

    # 3) Abre PRs via Azure DevOps REST API (se houver credencial)
    if settings.azdo_auth_mode == "sp" and app_token:
        pr_client = AzdoClient(organization=settings.azdo_org, bearer_token=app_token)
    elif settings.azdo_pat:
        pr_client = AzdoClient(organization=settings.azdo_org, pat=settings.azdo_pat)
    else:
        logger.warning("Sem credencial para abrir PR - branches enviadas sem PR automatico.")
        session.execution = execution
        return ToolResult.success(execution)

    with pr_client as client:
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
