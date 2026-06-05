"""Tool dispatcher: mapeia tool calls do LLM para handlers Python tipados.

Cada handler:
- Valida argumentos do LLM (Pydantic)
- Executa a tool deterministica correspondente
- Atualiza o SessionState (side effect controlado)
- Retorna um dict serializavel para devolver ao LLM como tool result
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from devops_agent.config import Settings
from devops_agent.llm.client import ToolCall
from devops_agent.models import AppType, Language, Phase, SessionState
from devops_agent.orchestrator import executor as exec_mod
from devops_agent.tools.validation import (
    validate_app_type,
    validate_branch_name,
    validate_k8s_namespace,
    validate_language,
    validate_repo_url,
)

logger = logging.getLogger(__name__)


# ----- argument schemas (validados antes de chamar tool) -----


class _RepoArgs(BaseModel):
    url: str


class _BranchArgs(BaseModel):
    branch: str


class _NamespaceArgs(BaseModel):
    namespace: str


class _AppTypeArgs(BaseModel):
    value: str


class _LanguageArgs(BaseModel):
    language: str


class _ExecuteArgs(BaseModel):
    user_confirmation: str


class _AbortArgs(BaseModel):
    reason: str


# ----- handlers -----

Handler = Callable[[dict[str, Any], SessionState, Settings], dict[str, Any]]


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _fail(error: str, code: str | None = None) -> dict[str, Any]:
    return {"ok": False, "error": error, "error_code": code}


def _handle_validate_repo_url(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _RepoArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    r = validate_repo_url(parsed.url, allowed_org=settings.azdo_org)
    if not r.ok or r.data is None:
        return _fail(r.error or "url invalida", r.error_code)

    session.repo_url = parsed.url.strip()
    session.repo_coords = r.data
    session.app_name = r.data.repository.lower()
    session.phase = Phase.ASK_BRANCH
    session.record_audit("repo_validated", repo=r.data.full_name)
    return _ok(
        {
            "organization": r.data.organization,
            "project": r.data.project,
            "repository": r.data.repository,
            "next_question": "Em qual branch devo adicionar os arquivos da pipeline?",
        }
    )


def _handle_validate_branch(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _BranchArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    r = validate_branch_name(parsed.branch)
    if not r.ok or r.data is None:
        return _fail(r.error or "branch invalida", r.error_code)

    session.branch = r.data
    session.phase = Phase.ASK_NAMESPACE
    return _ok(
        {
            "branch": r.data,
            "next_question": "Qual o namespace Kubernetes onde a aplicacao deve ficar?",
        }
    )


def _handle_validate_namespace(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _NamespaceArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    r = validate_k8s_namespace(parsed.namespace)
    if not r.ok or r.data is None:
        return _fail(r.error or "namespace invalido", r.error_code)

    session.namespace = r.data
    session.phase = Phase.ASK_APP_TYPE
    return _ok(
        {
            "namespace": r.data,
            "next_question": "Sua aplicacao e API, Worker ou Web?",
        }
    )


def _handle_validate_app_type(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _AppTypeArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    r = validate_app_type(parsed.value)
    if not r.ok or r.data is None:
        return _fail(r.error or "tipo invalido", r.error_code)

    session.app_type = r.data
    # web nao detecta linguagem; demais tipos passam por detect_language antes do scaffold.
    session.phase = Phase.VALIDATE if r.data == AppType.WEB else Phase.DETECT_LANGUAGE
    return _ok({"app_type": r.data.value})


def _handle_detect_language(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    if not session.repo_coords or not session.branch:
        return _fail(
            "detecte a linguagem apenas apos validar repo e branch.",
            "detect.prereq_missing",
        )

    result = exec_mod.detect_language_step(session, settings)
    det = result.data
    if det is None:
        return _fail(result.error or "falha na deteccao de linguagem", result.error_code)

    return _ok(
        {
            "is_conclusive": det.is_conclusive,
            "language": det.language.value if det.language else None,
            "candidates": [c.value for c in det.candidates],
            "next": (
                "chame propose_scaffold"
                if det.is_conclusive
                else "pergunte a linguagem ao usuario e chame set_language"
            ),
        }
    )


def _handle_set_language(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _LanguageArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    if session.phase not in (Phase.DETECT_LANGUAGE, Phase.ASK_LANGUAGE, Phase.VALIDATE):
        return _fail(
            "set_language so pode ser chamada apos detect_language.",
            "language.wrong_phase",
        )

    r = validate_language(parsed.language)
    if not r.ok or r.data is None:
        return _fail(r.error or "linguagem invalida", r.error_code)

    session.language = r.data
    session.phase = Phase.VALIDATE
    return _ok({"language": r.data.value})


def _handle_propose_scaffold(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    result = exec_mod.prepare_dry_run(session, settings)
    if not result.ok or result.data is None:
        return _fail(result.error or "falha no dry_run", result.error_code)

    bundle = result.data.bundle
    session.phase = Phase.CONFIRM
    return _ok(
        {
            "app_repo_files": [f.path for f in bundle.app_repo_files],
            "helm_chart_files": [f.path for f in bundle.helm_chart_files],
            "app_repo_collisions": result.data.app_repo_collisions,
            "helm_repo_collisions": result.data.helm_repo_collisions,
            "has_collisions": result.data.has_collisions,
            "summary_to_show_user": exec_mod.render_summary(bundle),
            "confirm_prompt": "Confirma a criacao destes arquivos? (sim/nao)",
        }
    )


def _handle_execute_scaffold(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _ExecuteArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    if session.phase != Phase.CONFIRM:
        return _fail(
            "execute_scaffold so pode ser chamada apos propose_scaffold e confirmacao.",
            "execute.wrong_phase",
        )

    session.confirmed = True
    session.record_audit("user_confirmed", text=parsed.user_confirmation)
    session.phase = Phase.EXECUTE

    workdir = Path(settings.workdir_root) if settings.workdir_root else None
    result = exec_mod.execute(session, settings, workdir_root=workdir)
    if not result.ok or result.data is None:
        session.phase = Phase.ERROR
        return _fail(result.error or "execute falhou", result.error_code)

    session.phase = Phase.DONE
    return _ok(
        {
            "app_pr_url": result.data.app_pr_url,
            "helm_pr_url": result.data.helm_pr_url,
            "app_branch": result.data.app_branch,
            "helm_branch": result.data.helm_branch,
        }
    )


def _handle_abort_session(
    args: dict[str, Any], session: SessionState, settings: Settings
) -> dict[str, Any]:
    try:
        parsed = _AbortArgs.model_validate(args)
    except ValidationError as exc:
        return _fail(f"argumentos invalidos: {exc.errors()[0]['msg']}", "args.invalid")

    session.phase = Phase.ABORTED
    session.record_audit("session_aborted", reason=parsed.reason)
    return _ok({"phase": session.phase.value, "reason": parsed.reason})


_HANDLERS: dict[str, Handler] = {
    "validate_repo_url": _handle_validate_repo_url,
    "validate_branch": _handle_validate_branch,
    "validate_namespace": _handle_validate_namespace,
    "validate_app_type": _handle_validate_app_type,
    "detect_language": _handle_detect_language,
    "set_language": _handle_set_language,
    "propose_scaffold": _handle_propose_scaffold,
    "execute_scaffold": _handle_execute_scaffold,
    "abort_session": _handle_abort_session,
}


def dispatch(
    tool_call: ToolCall, session: SessionState, settings: Settings
) -> dict[str, Any]:
    """Despacha uma tool call para o handler correspondente. Nunca propaga exception."""
    handler = _HANDLERS.get(tool_call.name)
    if handler is None:
        return _fail(
            f"tool '{tool_call.name}' nao existe ou nao esta disponivel.",
            "tool.unknown",
        )
    try:
        return handler(tool_call.arguments, session, settings)
    except Exception as exc:  # noqa: BLE001 - barreira ao LLM
        logger.exception("handler %s lancou excecao", tool_call.name)
        return _fail(f"erro interno na tool: {exc}", "tool.internal_error")
