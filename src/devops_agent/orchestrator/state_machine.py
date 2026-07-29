"""Maquina de estados pura para o fluxo conversacional.

Implementada sem LangGraph para destravar a POC. Cada handler recebe o estado
atual + input e retorna o proximo estado + mensagem ao usuario + flags de
continuacao. LangGraph entra junto com a integracao do LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from devops_agent.config import Settings
from devops_agent.models import AppType, Language, Phase, SessionState
from devops_agent.tools.scaffold import (
    default_apim_path,
    default_ingress_host,
    default_python_entrypoint,
    strip_namespace_prefix,
)
from devops_agent.tools.validation import (
    validate_api_ids,
    validate_apim_path,
    validate_app_type,
    validate_application_name,
    validate_branch_name,
    validate_cron_schedule,
    validate_dotnet_entrypoint,
    validate_dotnet_version,
    validate_ingress_host,
    validate_k8s_namespace,
    validate_language,
    validate_python_entrypoint,
    validate_python_package_manager,
    validate_python_version,
    validate_repo_url,
    validate_yes_no,
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
    Phase.ASK_SCHEDULE: "Qual o agendamento (cron) do CronJob? Ex.: '0 2 * * *' ou @daily.",
    Phase.ASK_LANGUAGE: "Nao consegui detectar a linguagem. Qual a linguagem da aplicacao?",
    Phase.ASK_DOTNET_VERSION: "Qual a versao do .NET? Ex.: 8.0.x (enter usa o default 8.0.x).",
    Phase.ASK_DOTNET_ENTRYPOINT: (
        "Qual o assembly de entrada (.dll)? Ex.: ePharma.Authentication.Services.dll "
        "(enter usa {app_name}.dll)."
    ),
    Phase.ASK_PYTHON_VERSION: "Qual a versao do Python? Ex.: 3.12 (enter usa o default 3.12).",
    Phase.ASK_PYTHON_PACKAGE_MANAGER: (
        "O projeto usa pip (requirements.txt) ou poetry (pyproject.toml)? "
        "(enter usa pip)."
    ),
    Phase.ASK_PYTHON_ENTRYPOINT: (
        "Qual o comando que inicia a aplicacao no container? "
        "Ex.: uvicorn main:app --host 0.0.0.0 --port 8080 "
        "(enter usa o default do tipo de aplicacao)."
    ),
    Phase.ASK_APPLICATION_NAME: (
        "Qual o applicationName (nome curto da aplicacao)? Ex.: api-utils "
        "(enter usa o nome do repositorio)."
    ),
    Phase.ASK_USES_APIM: (
        "A aplicacao vai ser exposta no APIM? (sim/nao)\n"
        "  'nao' emite enableApim: false e pula as perguntas de path e apiId."
    ),
    Phase.ASK_APIM_PATH: (
        "Qual o pathApiManagementService (path no APIM)? Ex.: utils "
        "(enter deriva do applicationName, sem o prefixo 'api-')."
    ),
    Phase.ASK_API_IDS: (
        "Quais os apiIds do APIM (obrigatorio - o template-aks exige um por ambiente)?\n"
        "  - um valor unico e aplicado aos quatro ambientes. Ex.: api-utils\n"
        "  - ou os quatro separados por virgula, na ordem dev,qa,uat,prod. "
        "Ex.: api-utils-dev,api-utils-qa,api-utils-uat,api-utils"
    ),
    Phase.ASK_INGRESS_HOST: (
        "Qual o host base do ingress? Ex.: servicesutils "
        "(o dominio .k8s.<ambiente>.epharma.com.br e adicionado automaticamente; "
        "enter deriva de 'services<path>')."
    ),
}


# O template-aks-python declara um unico parametro `apiId`; a pergunta muda de forma
# para nao sugerir que da para ter um apiId por ambiente nesse stack.
# Primeira pergunta especifica de cada stack com template real. Linguagens sem entrada
# aqui usam o template generico (_default) e vao direto para a validacao.
_FIRST_PHASE_BY_LANGUAGE: dict[Language, Phase] = {
    Language.DOTNET: Phase.ASK_DOTNET_VERSION,
    Language.PYTHON: Phase.ASK_PYTHON_VERSION,
}


def first_phase_for_language(language: Language) -> Phase | None:
    return _FIRST_PHASE_BY_LANGUAGE.get(language)


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
    # Usa o microservice-chart (com ingresses), entao ainda perguntamos o host.
    if result.data == AppType.WEB:
        session.phase = Phase.ASK_INGRESS_HOST
        return StepResult(
            next_phase=Phase.ASK_INGRESS_HOST,
            message_to_user=f"Tipo: {result.data.value}\n" + _QUESTIONS[Phase.ASK_INGRESS_HOST],
        )

    # cronjob precisa do agendamento antes de prosseguir.
    if result.data == AppType.CRONJOB:
        session.phase = Phase.ASK_SCHEDULE
        return StepResult(
            next_phase=Phase.ASK_SCHEDULE,
            message_to_user=f"Tipo: {result.data.value}\n" + _QUESTIONS[Phase.ASK_SCHEDULE],
        )

    session.phase = Phase.DETECT_LANGUAGE
    return StepResult(
        next_phase=Phase.DETECT_LANGUAGE,
        message_to_user=f"Tipo: {result.data.value}\nDetectando a linguagem...",
        awaits_input=False,
    )


def handle_schedule(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia mantem o default do scaffold (session.schedule permanece None).
    if not user_input.strip():
        session.phase = Phase.DETECT_LANGUAGE
        return StepResult(
            next_phase=Phase.DETECT_LANGUAGE,
            message_to_user="Mantendo o agendamento padrao.\nDetectando a linguagem...",
            awaits_input=False,
        )

    result = validate_cron_schedule(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_SCHEDULE,
            message_to_user=result.error or "Agendamento invalido.",
            error=result.error,
        )

    session.schedule = result.data
    session.phase = Phase.DETECT_LANGUAGE
    return StepResult(
        next_phase=Phase.DETECT_LANGUAGE,
        message_to_user=f"Agendamento: {result.data}\nDetectando a linguagem...",
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

    # Stacks com template real (dotnet/python) coletam os parametros do contrato antes
    # de validar; os demais caem no template generico e nao precisam de mais nada.
    first_phase = first_phase_for_language(result.data)
    if first_phase is not None:
        session.phase = first_phase
        return StepResult(
            next_phase=first_phase,
            message_to_user=f"Linguagem: {result.data.value}\n" + _QUESTIONS[first_phase],
        )

    session.phase = Phase.VALIDATE
    return StepResult(
        next_phase=Phase.VALIDATE,
        message_to_user=f"Linguagem: {result.data.value}\nValidando inputs...",
        awaits_input=False,
    )


def handle_dotnet_version(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia mantem o default do scaffold (session.dotnet_version permanece None).
    if not user_input.strip():
        session.phase = Phase.ASK_DOTNET_ENTRYPOINT
        return StepResult(
            next_phase=Phase.ASK_DOTNET_ENTRYPOINT,
            message_to_user="Mantendo a versao padrao do .NET.\n" + _QUESTIONS[Phase.ASK_DOTNET_ENTRYPOINT],
        )

    result = validate_dotnet_version(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_DOTNET_VERSION,
            message_to_user=result.error or "Versao .NET invalida.",
            error=result.error,
        )

    session.dotnet_version = result.data
    session.phase = Phase.ASK_DOTNET_ENTRYPOINT
    return StepResult(
        next_phase=Phase.ASK_DOTNET_ENTRYPOINT,
        message_to_user=f"Versao .NET: {result.data}\n" + _QUESTIONS[Phase.ASK_DOTNET_ENTRYPOINT],
    )


def handle_dotnet_entrypoint(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia mantem o default do scaffold (app_name.dll).
    if not user_input.strip():
        session.phase = Phase.ASK_APPLICATION_NAME
        return StepResult(
            next_phase=Phase.ASK_APPLICATION_NAME,
            message_to_user="Usando o assembly padrao.\n" + _QUESTIONS[Phase.ASK_APPLICATION_NAME],
        )

    result = validate_dotnet_entrypoint(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_DOTNET_ENTRYPOINT,
            message_to_user=result.error or "Assembly invalido.",
            error=result.error,
        )

    session.dotnet_entrypoint = result.data
    session.phase = Phase.ASK_APPLICATION_NAME
    return StepResult(
        next_phase=Phase.ASK_APPLICATION_NAME,
        message_to_user=f"Entrypoint: {result.data}\n" + _QUESTIONS[Phase.ASK_APPLICATION_NAME],
    )


def handle_python_version(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia mantem o default do scaffold (session.python_version permanece None).
    if not user_input.strip():
        session.phase = Phase.ASK_PYTHON_PACKAGE_MANAGER
        return StepResult(
            next_phase=Phase.ASK_PYTHON_PACKAGE_MANAGER,
            message_to_user="Mantendo a versao padrao do Python.\n"
            + _QUESTIONS[Phase.ASK_PYTHON_PACKAGE_MANAGER],
        )

    result = validate_python_version(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_PYTHON_VERSION,
            message_to_user=result.error or "Versao Python invalida.",
            error=result.error,
        )

    session.python_version = result.data
    session.phase = Phase.ASK_PYTHON_PACKAGE_MANAGER
    return StepResult(
        next_phase=Phase.ASK_PYTHON_PACKAGE_MANAGER,
        message_to_user=f"Versao Python: {result.data}\n"
        + _QUESTIONS[Phase.ASK_PYTHON_PACKAGE_MANAGER],
    )


def handle_python_package_manager(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia mantem o default do scaffold (pip).
    if not user_input.strip():
        session.phase = Phase.ASK_PYTHON_ENTRYPOINT
        return StepResult(
            next_phase=Phase.ASK_PYTHON_ENTRYPOINT,
            message_to_user="Usando pip (requirements.txt).\n"
            + _QUESTIONS[Phase.ASK_PYTHON_ENTRYPOINT],
        )

    result = validate_python_package_manager(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_PYTHON_PACKAGE_MANAGER,
            message_to_user=result.error or "Gerenciador invalido.",
            error=result.error,
        )

    session.python_package_manager = result.data
    session.phase = Phase.ASK_PYTHON_ENTRYPOINT
    return StepResult(
        next_phase=Phase.ASK_PYTHON_ENTRYPOINT,
        message_to_user=f"Gerenciador: {result.data}\n" + _QUESTIONS[Phase.ASK_PYTHON_ENTRYPOINT],
    )


def handle_python_entrypoint(session: SessionState, user_input: str) -> StepResult:
    # Entrada vazia usa o default por tipo de aplicacao (ver default_python_entrypoint).
    if not user_input.strip():
        session.python_entrypoint = default_python_entrypoint(session.app_type or AppType.API)
        return _after_python_entrypoint(session)

    result = validate_python_entrypoint(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_PYTHON_ENTRYPOINT,
            message_to_user=result.error or "Entrypoint invalido.",
            error=result.error,
        )

    session.python_entrypoint = result.data
    return _after_python_entrypoint(session)


def _after_python_entrypoint(session: SessionState) -> StepResult:
    session.phase = Phase.ASK_APPLICATION_NAME
    return StepResult(
        next_phase=Phase.ASK_APPLICATION_NAME,
        message_to_user=f"Entrypoint: {session.python_entrypoint}\n"
        + _QUESTIONS[Phase.ASK_APPLICATION_NAME],
    )


def _after_application_name(session: SessionState) -> StepResult:
    """Apos o applicationName: api pergunta se usa APIM; os demais validam direto."""
    if session.app_type == AppType.API:
        session.phase = Phase.ASK_USES_APIM
        return StepResult(
            next_phase=Phase.ASK_USES_APIM,
            message_to_user=_QUESTIONS[Phase.ASK_USES_APIM],
        )
    session.phase = Phase.VALIDATE
    return StepResult(
        next_phase=Phase.VALIDATE,
        message_to_user="Validando inputs...",
        awaits_input=False,
    )


def handle_uses_apim(session: SessionState, user_input: str) -> StepResult:
    result = validate_yes_no(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_USES_APIM,
            message_to_user=result.error or "Responda sim ou nao.",
            error=result.error,
        )

    session.uses_apim = result.data
    if result.data:
        session.phase = Phase.ASK_APIM_PATH
        return StepResult(
            next_phase=Phase.ASK_APIM_PATH,
            message_to_user="APIM: sim\n" + _QUESTIONS[Phase.ASK_APIM_PATH],
        )

    # Sem APIM: path e apiId nao sao perguntados, mas o ingress continua existindo.
    session.phase = Phase.ASK_INGRESS_HOST
    return StepResult(
        next_phase=Phase.ASK_INGRESS_HOST,
        message_to_user="APIM: nao (enableApim: false)\n" + _QUESTIONS[Phase.ASK_INGRESS_HOST],
    )


def handle_application_name(session: SessionState, user_input: str) -> StepResult:
    namespace = session.namespace or ""
    # Entrada vazia usa o nome do repositorio (app_name).
    if not user_input.strip():
        session.application_name = strip_namespace_prefix(session.app_name or "", namespace)
        return _after_application_name(session)

    result = validate_application_name(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_APPLICATION_NAME,
            message_to_user=result.error or "applicationName invalido.",
            error=result.error,
        )

    # Remove o prefixo do namespace (ex.: ns=autorizador, nome=autorizador-api-teste -> api-teste),
    # para nao duplicar o namespace no serviceAccount (sa-<ns>-<applicationName>).
    session.application_name = strip_namespace_prefix(result.data, namespace)
    return _after_application_name(session)


def handle_apim_path(session: SessionState, user_input: str) -> StepResult:
    application_name = session.application_name or session.app_name or ""
    # Entrada vazia deriva do applicationName (sem o prefixo 'api-').
    if not user_input.strip():
        session.apim_path = default_apim_path(application_name)
        session.phase = Phase.ASK_API_IDS
        return StepResult(
            next_phase=Phase.ASK_API_IDS,
            message_to_user=f"Path do APIM: {session.apim_path}\n"
            + _QUESTIONS[Phase.ASK_API_IDS],
        )

    result = validate_apim_path(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_APIM_PATH,
            message_to_user=result.error or "pathApiManagementService invalido.",
            error=result.error,
        )

    session.apim_path = result.data
    session.phase = Phase.ASK_API_IDS
    return StepResult(
        next_phase=Phase.ASK_API_IDS,
        message_to_user=f"Path do APIM: {result.data}\n" + _QUESTIONS[Phase.ASK_API_IDS],
    )


def handle_api_ids(session: SessionState, user_input: str) -> StepResult:
    # Obrigatorio (sem default): entrada vazia re-pergunta.
    result = validate_api_ids(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_API_IDS,
            message_to_user=result.error or "apiIds invalidos.",
            error=result.error,
        )

    session.api_ids = result.data
    session.phase = Phase.ASK_INGRESS_HOST
    return StepResult(
        next_phase=Phase.ASK_INGRESS_HOST,
        message_to_user=_resumo_api_ids(result.data) + "\n" + _QUESTIONS[Phase.ASK_INGRESS_HOST],
    )


def _resumo_api_ids(api_ids: dict[str, str]) -> str:
    """Eco dos apiIds resolvidos - explicita quando um valor unico cobre os quatro."""
    unicos = set(api_ids.values())
    if len(unicos) == 1:
        return f"apiId (o mesmo para {', '.join(api_ids)}): {unicos.pop()}"
    return "apiIds: " + ", ".join(f"{env}={api_id}" for env, api_id in api_ids.items())


def handle_ingress_host(session: SessionState, user_input: str) -> StepResult:
    # Base para derivar o default do host quando a entrada e vazia.
    base_path = session.apim_path or default_apim_path(
        session.application_name or session.app_name or ""
    )
    # Entrada vazia deriva de 'services<path>'.
    if not user_input.strip():
        session.ingress_host = default_ingress_host(base_path)
        session.phase = Phase.VALIDATE
        return StepResult(
            next_phase=Phase.VALIDATE,
            message_to_user=f"Host do ingress: {session.ingress_host}\nValidando inputs...",
            awaits_input=False,
        )

    result = validate_ingress_host(user_input)
    if not result.ok or result.data is None:
        return StepResult(
            next_phase=Phase.ASK_INGRESS_HOST,
            message_to_user=result.error or "Host invalido.",
            error=result.error,
        )

    session.ingress_host = result.data
    session.phase = Phase.VALIDATE
    return StepResult(
        next_phase=Phase.VALIDATE,
        message_to_user=f"Host do ingress: {result.data}\nValidando inputs...",
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
