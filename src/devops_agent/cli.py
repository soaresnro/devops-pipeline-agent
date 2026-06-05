"""CLI driver da POC.

Conduz as 4 perguntas usando a state machine deterministica.
Quando o LLM for integrado, este driver se torna apenas um dos clients (junto
com Teams bot e SPA).
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

    if session.phase == Phase.VALIDATE:
        result = exec_mod.prepare_dry_run(session, settings)  # type: ignore[arg-type]
        if not result.ok or result.data is None:
            _print_error(result.error or "Falha no dry_run.")
            session.phase = Phase.ERROR
            return False
        bundle = result.data.bundle
        _print_agent(exec_mod.render_summary(bundle))
        if result.data.has_collisions:
            collisions = (
                "Colisoes detectadas:\n"
                + "\n".join(f"  - {c}" for c in result.data.app_repo_collisions)
                + "\n"
                + "\n".join(f"  - {c}" for c in result.data.helm_repo_collisions)
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
        _print_agent("Executando: clonando repos, escrevendo arquivos, abrindo PRs...")
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
        if data.helm_pr_url:
            msg += f"PR do repo devops-templates: {data.helm_pr_url}\n"
        elif data.helm_branch:
            msg += f"Branch enviada (PR nao aberto - configure credencial): {data.helm_branch}\n"
        _print_agent(msg)
        session.phase = Phase.DONE
        return False

    return False


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
    """Inicia a sessao conversacional de scaffold."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        _print_error(
            f"Configuracao invalida: {exc}\n"
            "Verifique seu arquivo .env (use .env.example como referencia)."
        )
        raise typer.Exit(code=2) from exc

    if settings.azdo_auth_mode == "sp" and not user_token:
        _print_error(
            "Modo de auth 'sp' exige o token do usuario para o check de permissao (OBO).\n"
            "Passe --user-token <jwt> ou defina AZDO_AGENT_USER_ASSERTION."
        )
        raise typer.Exit(code=2)

    session = SessionState(
        requested_by=requested_by or getpass.getuser(),
        user_assertion=user_token or None,
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
    """Sessao conversacional com o LLM (Azure OpenAI)."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        _print_error(f"Configuracao invalida: {exc}")
        raise typer.Exit(code=2) from exc

    if settings.azdo_auth_mode == "sp" and not user_token:
        _print_error(
            "Modo de auth 'sp' exige o token do usuario para o check de permissao (OBO).\n"
            "Passe --user-token <jwt> ou defina AZDO_AGENT_USER_ASSERTION."
        )
        raise typer.Exit(code=2)

    if not settings.llm_enabled:
        _print_error(
            "AZURE_OPENAI_ENDPOINT e AZURE_OPENAI_API_KEY nao configurados.\n"
            "Configure-os no .env ou use 'devops-agent scaffold' (modo deterministico)."
        )
        raise typer.Exit(code=2)

    try:
        from devops_agent.llm import Agent, AgentError, AzureOpenAIClient
    except RuntimeError as exc:
        _print_error(
            f"{exc}\nReinstale com extras: pip install -e \".[llm]\""
        )
        raise typer.Exit(code=2) from exc

    llm = AzureOpenAIClient(
        endpoint=settings.azure_openai_endpoint or "",
        api_key=settings.azure_openai_api_key or "",
        deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
    )

    session = SessionState(
        requested_by=requested_by or getpass.getuser(),
        user_assertion=user_token or None,
        user_oid=user_oid or None,
    )
    console.print(f"[dim]session={session.session_id}[/dim]")

    agent = Agent(session=session, settings=settings, llm=llm)

    try:
        greeting = agent.start()
        _print_agent(greeting)

        while not agent.is_finished():
            user_text = _ask(">")
            if not user_text:
                continue
            try:
                response = agent.send(user_text)
            except AgentError as exc:
                _print_error(f"Limite excedido: {exc}")
                break
            _print_agent(response)
    except KeyboardInterrupt:
        _print_agent("Sessao interrompida pelo usuario.")
        raise typer.Exit(code=130) from None

    if session.phase == Phase.ERROR:
        sys.exit(1)
    if session.phase == Phase.ABORTED:
        sys.exit(2)


@app.command()
def version() -> None:
    """Mostra a versao instalada."""
    from devops_agent import __version__

    console.print(f"devops-pipeline-agent {__version__}")


if __name__ == "__main__":
    app()
