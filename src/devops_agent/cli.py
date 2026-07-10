"""CLI driver da POC.

Conduz as perguntas usando a state machine deterministica (sem LLM). Os comandos
`scaffold` e `chat` rodam exatamente o mesmo fluxo - `chat` e mantido como alias
por compatibilidade. Quando houver um front-end (Teams bot / SPA), este driver se
torna apenas um dos clients.
"""

from __future__ import annotations

import getpass
import logging
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from devops_agent.config import Settings, get_settings
from devops_agent.models import AppType, Language, Phase, SessionState
from devops_agent.orchestrator import executor as exec_mod
from devops_agent.orchestrator import state_machine as sm
from devops_agent.tools import auth

app = typer.Typer(
    add_completion=False,
    help="Agente de scaffold de pipelines CI/CD para Azure DevOps.",
    no_args_is_help=True,
)
console = Console()


def _print_agent(message: str) -> None:
    console.print(Panel(message, title="agente", border_style="cyan", padding=(0, 1)))


def _print_error(message: str) -> None:
    console.print(Panel(message, title="erro", border_style="red", padding=(0, 1)))


def _ask(prompt: str) -> str:
    return Prompt.ask(f"[bold yellow]voce[/bold yellow] {prompt}").strip()


def _resolve_user_assertion(settings: Settings, user_token: str) -> str | None:
    """Resolve a assercao do usuario para o modo sp (precedencia: token > login).

    Token explicito (--user-token/env) sempre vence. Senao, no modo sp, faz login
    interativo (device code) se AZDO_LOGIN_CLIENT_ID estiver configurado. Caso
    contrario, encerra com orientacao. Fora do modo sp, retorna None.
    """
    if user_token:
        return user_token
    if settings.azdo_auth_mode != "sp":
        return None
    if settings.azdo_login_client_id:
        _print_agent("Autenticacao necessaria - siga o codigo/URL exibidos para fazer login.")
        result = auth.acquire_user_assertion_interactive(settings)
        if not result.ok or result.data is None:
            _print_error(result.error or "Falha no login interativo.")
            raise typer.Exit(code=2)
        return result.data
    _print_error(
        "Modo de auth 'sp' exige o token do usuario para o check de permissao (OBO).\n"
        "Passe --user-token <jwt>, defina AZDO_AGENT_USER_ASSERTION, ou configure "
        "AZDO_LOGIN_CLIENT_ID para login interativo."
    )
    raise typer.Exit(code=2)


def _run_phase(session: SessionState, settings: object) -> bool:
    """Roda uma transicao da state machine. Retorna False quando termina o fluxo."""
    if session.phase == Phase.GREET:
        step = sm.greet(session)
        _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_REPO:
        raw = _ask(">")
        step = sm.handle_repo_url(session, raw, settings)  # type: ignore[arg-type]
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_BRANCH:
        raw = _ask(">")
        step = sm.handle_branch(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_NAMESPACE:
        raw = _ask(">")
        step = sm.handle_namespace(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_APP_TYPE:
        raw = _ask(f"({' / '.join(t.value for t in AppType)}) >")
        step = sm.handle_app_type(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_SCHEDULE:
        _print_agent(sm.question_for(Phase.ASK_SCHEDULE))
        raw = _ask("(enter para manter '0 * * * *') >")
        step = sm.handle_schedule(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.DETECT_LANGUAGE:
        # Fase transitoria: detecta via API (sem input) e segue para VALIDATE ou ASK_LANGUAGE.
        detection = exec_mod.detect_language_step(session, settings).data  # type: ignore[arg-type]
        if detection is not None and detection.is_conclusive and detection.language is not None:
            _print_agent(f"Linguagem detectada: {detection.language.value}")
        return True

    if session.phase == Phase.ASK_LANGUAGE:
        if session.language_candidates:
            _print_agent(
                "Encontrei marcadores de mais de uma linguagem: "
                + ", ".join(c.value for c in session.language_candidates)
                + ". Qual devo usar?"
            )
        else:
            _print_agent(sm.question_for(Phase.ASK_LANGUAGE))
        raw = _ask(f"({' / '.join(lang.value for lang in Language)}) >")
        step = sm.handle_language(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_DOTNET_VERSION:
        _print_agent(sm.question_for(Phase.ASK_DOTNET_VERSION))
        raw = _ask("(enter para 8.0.x) >")
        step = sm.handle_dotnet_version(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_DOTNET_ENTRYPOINT:
        _print_agent(sm.question_for(Phase.ASK_DOTNET_ENTRYPOINT))
        raw = _ask(f"(enter para {session.app_name}.dll) >")
        step = sm.handle_dotnet_entrypoint(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_APPLICATION_NAME:
        _print_agent(sm.question_for(Phase.ASK_APPLICATION_NAME))
        raw = _ask(f"(enter para {session.app_name}) >")
        step = sm.handle_application_name(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_APIM_PATH:
        _print_agent(sm.question_for(Phase.ASK_APIM_PATH))
        raw = _ask("(enter para derivar do applicationName) >")
        step = sm.handle_apim_path(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.ASK_INGRESS_HOST:
        _print_agent(sm.question_for(Phase.ASK_INGRESS_HOST))
        raw = _ask("(enter para derivar de 'services<path>') >")
        step = sm.handle_ingress_host(session, raw)
        if step.error:
            _print_error(step.message_to_user)
        else:
            _print_agent(step.message_to_user)
        return True

    if session.phase == Phase.VALIDATE:
        result = exec_mod.prepare_dry_run(session, settings)  # type: ignore[arg-type]
        if not result.ok or result.data is None:
            _print_error(result.error or "Falha no dry_run.")
            session.phase = Phase.ERROR
            return False
        bundle = result.data.bundle
        _print_agent(exec_mod.render_summary(bundle))
        if result.data.has_collisions:
            collisions = "Colisoes detectadas:\n" + "\n".join(
                f"  - {c}" for c in result.data.collisions
            )
            _print_error(collisions + "\n\nAbortando.")
            session.phase = Phase.ABORTED
            return False
        session.phase = Phase.CONFIRM
        return True

    if session.phase == Phase.CONFIRM:
        ans = _ask("confirma? (sim/nao) >").lower()
        if ans in {"sim", "s", "yes", "y"}:
            session.confirmed = True
            session.phase = Phase.EXECUTE
        else:
            _print_agent("Ok, sessao cancelada sem alteracoes.")
            session.phase = Phase.ABORTED
            return False
        return True

    if session.phase == Phase.EXECUTE:
        _print_agent("Executando: clonando o repo, escrevendo arquivos, abrindo PR...")
        exec_result = exec_mod.execute(session, settings)  # type: ignore[arg-type]
        if not exec_result.ok or exec_result.data is None:
            _print_error(exec_result.error or "Falha no execute.")
            session.phase = Phase.ERROR
            return False
        data = exec_result.data
        msg = "Pronto!\n"
        if data.app_pr_url:
            msg += f"PR do repo da aplicacao: {data.app_pr_url}\n"
        elif data.app_branch:
            msg += f"Branch enviada (PR nao aberto - configure credencial): {data.app_branch}\n"
        _print_agent(msg)
        session.phase = Phase.DONE
        return False

    return False


def _load_settings() -> Settings:
    try:
        return get_settings()
    except Exception as exc:  # noqa: BLE001
        _print_error(
            f"Configuracao invalida: {exc}\n"
            "Verifique seu arquivo .env (use .env.example como referencia)."
        )
        raise typer.Exit(code=2) from exc


def _run_deterministic_session(
    settings: Settings,
    *,
    requested_by: str,
    user_token: str,
    user_oid: str,
) -> None:
    """Conduz a sessao de scaffold pela state machine deterministica (sem LLM)."""
    user_assertion = _resolve_user_assertion(settings, user_token)

    session = SessionState(
        requested_by=requested_by or getpass.getuser(),
        user_assertion=user_assertion,
        user_oid=user_oid or None,
    )
    console.print(f"[dim]session={session.session_id}[/dim]")

    try:
        while _run_phase(session, settings):
            pass
    except KeyboardInterrupt:
        _print_agent("Sessao interrompida pelo usuario.")
        session.phase = Phase.ABORTED
        raise typer.Exit(code=130) from None

    if session.phase == Phase.ERROR:
        sys.exit(1)
    if session.phase == Phase.ABORTED:
        sys.exit(2)


@app.command()
def scaffold(
    requested_by: str = typer.Option(
        "",
        "--requested-by",
        help="Identificacao do usuario humano (email/oid) para auditoria.",
    ),
    user_token: str = typer.Option(
        "",
        "--user-token",
        envvar="AZDO_AGENT_USER_ASSERTION",
        help="Token de assercao do usuario (JWT) para o fluxo OBO no modo de auth 'sp'.",
    ),
    user_oid: str = typer.Option(
        "",
        "--user-oid",
        help="Object ID (oid) do usuario humano, para auditoria.",
    ),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Inicia a sessao de scaffold deterministica (perguntas fixas, sem LLM)."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = _load_settings()
    _run_deterministic_session(
        settings, requested_by=requested_by, user_token=user_token, user_oid=user_oid
    )


@app.command()
def chat(
    requested_by: str = typer.Option(
        "",
        "--requested-by",
        help="Identificacao do usuario humano (email/oid) para auditoria.",
    ),
    user_token: str = typer.Option(
        "",
        "--user-token",
        envvar="AZDO_AGENT_USER_ASSERTION",
        help="Token de assercao do usuario (JWT) para o fluxo OBO no modo de auth 'sp'.",
    ),
    user_oid: str = typer.Option(
        "",
        "--user-oid",
        help="Object ID (oid) do usuario humano, para auditoria.",
    ),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Alias de `scaffold`: roda o fluxo de scaffold deterministico (sem LLM)."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = _load_settings()
    _print_agent("Modo deterministico (sem LLM) - equivalente a 'devops-agent scaffold'.")
    _run_deterministic_session(
        settings, requested_by=requested_by, user_token=user_token, user_oid=user_oid
    )


@app.command()
def version() -> None:
    """Mostra a versao instalada."""
    from devops_agent import __version__

    console.print(f"devops-pipeline-agent {__version__}")


if __name__ == "__main__":
    app()
