"""Maquina de estados pura para o fluxo conversacional.

Implementada sem LangGraph para destravar a POC. Cada handler recebe o estado
atual + input e retorna o proximo estado + mensagem ao usuario + flags de
continuacao. LangGraph entra junto com a integracao do LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from devops_agent.config import Settings
from devops_agent.models import AppType, Phase, SessionState
from devops_agent.tools.validation import (
    validate_app_type,
    validate_branch_name,
    validate_k8s_namespace,
    validate_language,
    validate_repo_url,
)


@dataclass
class StepResult:
    """Resultado de um passo da state machine."""

    next_phase: Phase
    message_to_user: str
    awaits_input: bool = True
    error: str | None = None


_QUESTIONS = {
    Phase.ASK_REPO: "Qual o link do repositorio onde voce precisa da pipeline?",
    Phase.ASK_BRANCH: "Em qual branch devo adicionar os arquivos da pipeline?",
    Phase.ASK_NAMESPACE: "Qual o namespace Kubernetes onde a aplicacao deve ficar?",
    Phase.ASK_APP_TYPE: "Sua aplicacao e API, Worker, Web ou CronJob?",
    Phase.ASK_LANGUAGE: "Nao consegui detectar a linguagem. Qual a linguagem da aplicacao?",
}


def greet(session: SessionState) -> StepResult:
    session.phase = Phase.ASK_REPO
    return StepResult(
        next_phase=Phase.ASK_REPO,
        message_to_user=(
            "Ola! Vou te ajudar a criar a pipeline CI/CD desta aplicacao no AKS.\n"
            + _QUESTIONS[Phase.ASK_REPO]
        ),
    )


def handle_repo_url(session: SessionState, user_input: str, settings: Settings) -> StepResult:
    result = validate_repo_url(user_input, allowed_org=settings.azdo_org)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_REPO,
            message_to_user=result.error or "URL invalida.",
            error=result.error,
        )

    session.repo_url = user_input.strip()
    session.repo_coords = result.data
    session.app_name = result.data.repository.lower()
    session.phase = Phase.ASK_BRANCH
    session.record_audit("repo_validated", repo=result.data.full_name)
    return StepResult(
        next_phase=Phase.ASK_BRANCH,
        message_to_user=(
            f"Repo confirmado: {result.data.full_name}\n" + _QUESTIONS[Phase.ASK_BRANCH]
        ),
    )


def handle_branch(session: SessionState, user_input: str) -> StepResult:
    result = validate_branch_name(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_BRANCH,
            message_to_user=result.error or "Branch invalida.",
            error=result.error,
        )

    session.branch = result.data
    session.phase = Phase.ASK_NAMESPACE
    return StepResult(
        next_phase=Phase.ASK_NAMESPACE,
        message_to_user=f"Branch: {result.data}\n" + _QUESTIONS[Phase.ASK_NAMESPACE],
    )


def handle_namespace(session: SessionState, user_input: str) -> StepResult:
    result = validate_k8s_namespace(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_NAMESPACE,
            message_to_user=result.error or "Namespace invalido.",
            error=result.error,
        )

    session.namespace = result.data
    session.phase = Phase.ASK_APP_TYPE
    return StepResult(
        next_phase=Phase.ASK_APP_TYPE,
        message_to_user=f"Namespace: {result.data}\n" + _QUESTIONS[Phase.ASK_APP_TYPE],
    )


def handle_app_type(session: SessionState, user_input: str) -> StepResult:
    result = validate_app_type(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_APP_TYPE,
            message_to_user=result.error or "Tipo invalido.",
            error=result.error,
        )

    session.app_type = result.data

    # web e sempre uma SPA estatica (build Node -> nginx); nao detectamos linguagem.
    if result.data == AppType.WEB:
        session.phase = Phase.VALIDATE
        return StepResult(
            next_phase=Phase.VALIDATE,
            message_to_user=f"Tipo: {result.data.value}\nValidando inputs...",
            awaits_input=False,
        )

    session.phase = Phase.DETECT_LANGUAGE
    return StepResult(
        next_phase=Phase.DETECT_LANGUAGE,
        message_to_user=f"Tipo: {result.data.value}\nDetectando a linguagem...",
        awaits_input=False,
    )


def handle_language(session: SessionState, user_input: str) -> StepResult:
    result = validate_language(user_input)
    if not result.ok or result.data is None:
        # Permanece em ASK_LANGUAGE (nao volta a DETECT_LANGUAGE para nao re-bater na API).
        return StepResult(
            next_phase=Phase.ASK_LANGUAGE,
            message_to_user=result.error or "Linguagem invalida.",
            error=result.error,
        )

    session.language = result.data
    session.phase = Phase.VALIDATE
    return StepResult(
        next_phase=Phase.VALIDATE,
        message_to_user=f"Linguagem: {result.data.value}\nValidando inputs...",
        awaits_input=False,
    )


def question_for(phase: Phase) -> str:
    return _QUESTIONS.get(phase, "")


def all_inputs_ready(session: SessionState) -> bool:
    return all(
        [
            session.repo_coords is not None,
            session.branch is not None,
            session.namespace is not None,
            session.app_type is not None,
            session.app_name is not None,
        ]
    )


def collect_request(session: SessionState) -> tuple[str, str, str, AppType, str]:
    """Extrai os 5 valores essenciais. Levanta ValueError se algum falta."""
    if not all_inputs_ready(session):
        raise ValueError("Sessao incompleta para coleta de request.")
    assert session.repo_coords is not None
    assert session.branch is not None
    assert session.namespace is not None
    assert session.app_type is not None
    assert session.app_name is not None
    return (
        session.app_name,
        session.branch,
        session.namespace,
        session.app_type,
        session.repo_coords.full_name,
    )
