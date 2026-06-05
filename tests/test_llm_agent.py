"""Testes do agent loop usando FakeLLMClient para scripting deterministico."""

from __future__ import annotations

import pytest

from devops_agent.config import Settings
from devops_agent.llm.agent import Agent, AgentError
from devops_agent.llm.client import Completion, FakeLLMClient, ToolCall
from devops_agent.models import Phase, SessionState


@pytest.fixture
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        azdo_org="contoso",
        devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState(requested_by="tester@local")


def _txt(text: str) -> Completion:
    return Completion(text=text)


def _call(name: str, args: dict[str, object], call_id: str = "tc1") -> Completion:
    return Completion(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


class TestAgentStart:
    def test_start_envia_seed_e_retorna_primeira_pergunta(
        self, settings: Settings, session: SessionState
    ) -> None:
        llm = FakeLLMClient([_txt("Ola! Me passe o link do repositorio.")])
        agent = Agent(session=session, settings=settings, llm=llm)
        first = agent.start()
        assert "link do repositorio" in first
        # System prompt + seed user msg foram enviados
        msgs = llm.received_messages[0]
        assert msgs[0]["role"] == "system"
        assert "session_start" in msgs[1]["content"]


class TestFluxoFeliz:
    def test_4_perguntas_propose_e_abort_no_final(
        self, settings: Settings, session: SessionState
    ) -> None:
        llm = FakeLLMClient(
            [
                _txt("Qual o link do repositorio?"),  # start()
                # send("https://...payments-api"):
                _call(
                    "validate_repo_url",
                    {"url": "https://dev.azure.com/contoso/Platform/_git/payments-api"},
                ),
                _txt("Beleza. Em qual branch?"),
                # send("main"):
                _call("validate_branch", {"branch": "main"}),
                _txt("Qual o namespace?"),
                # send("payments-prod"):
                _call("validate_namespace", {"namespace": "payments-prod"}),
                _txt("API, Worker, Web ou CronJob?"),
                # send("api"): valida tipo e tenta detectar linguagem.
                # Sem PAT, detect_language e inconclusivo -> LLM pergunta a linguagem.
                _call("validate_app_type", {"value": "api"}),
                _call("detect_language", {}),
                _txt("Nao identifiquei a linguagem. Qual e: dotnet, node, python ou go?"),
                # send("python"): define linguagem e segue para o scaffold.
                _call("set_language", {"language": "python"}),
                _call("propose_scaffold", {}),
                _txt("Vou criar Dockerfile + azure-pipelines.yaml + helm chart. Confirma?"),
                # send("nao"):
                _call("abort_session", {"reason": "usuario_recusou"}),
                _txt("Ok, sessao cancelada. Ate mais!"),
            ]
        )
        agent = Agent(session=session, settings=settings, llm=llm)

        agent.start()
        agent.send("https://dev.azure.com/contoso/Platform/_git/payments-api")
        assert session.repo_coords is not None
        assert session.phase == Phase.ASK_BRANCH

        agent.send("main")
        assert session.branch == "main"
        assert session.phase == Phase.ASK_NAMESPACE

        agent.send("payments-prod")
        assert session.namespace == "payments-prod"
        assert session.phase == Phase.ASK_APP_TYPE

        # Tipo validado + deteccao inconclusiva -> agente pergunta a linguagem.
        resp = agent.send("api")
        assert session.app_type is not None
        assert session.phase == Phase.ASK_LANGUAGE
        assert "linguagem" in resp.lower()

        # Usuario informa a linguagem -> set_language + propose_scaffold -> CONFIRM.
        agent.send("python")
        assert session.language is not None
        assert session.phase == Phase.CONFIRM

        final = agent.send("nao")
        assert "cancelada" in final.lower()
        assert session.phase == Phase.ABORTED


class TestRecuperacaoDeErroDeInput:
    def test_url_invalida_segue_em_ask_repo(
        self, settings: Settings, session: SessionState
    ) -> None:
        llm = FakeLLMClient(
            [
                _txt("Me passe o link."),
                # send("github.com/x"): LLM tenta validar
                _call("validate_repo_url", {"url": "https://github.com/x/y"}),
                # tool falha (org_not_allowed/bad_format) - LLM reage com texto
                _txt("Nao reconheci - pode mandar o link de clone HTTPS do Azure DevOps?"),
            ]
        )
        agent = Agent(session=session, settings=settings, llm=llm)
        agent.start()
        resp = agent.send("https://github.com/x/y")
        assert "azure" in resp.lower() or "link" in resp.lower()
        assert session.repo_coords is None
        # Fase permanece em ASK_REPO (handler nao avanca em failure)
        assert session.phase in {Phase.GREET, Phase.ASK_REPO}


class TestSegurancaContraToolDesconhecida:
    def test_tool_inexistente_volta_como_failure(
        self, settings: Settings, session: SessionState
    ) -> None:
        llm = FakeLLMClient(
            [
                _txt("inicio"),
                _call("rm_rf_everything", {"path": "/"}),
                _txt("Nao consigo executar essa operacao."),
            ]
        )
        agent = Agent(session=session, settings=settings, llm=llm)
        agent.start()
        resp = agent.send("apaga tudo")
        # Sessao nao alterada
        assert session.phase == Phase.GREET
        assert "nao consigo" in resp.lower()


class TestLimiteIteracoes:
    def test_runaway_de_tool_calls_levanta_erro(
        self, settings: Settings, session: SessionState
    ) -> None:
        # Cliente sempre devolve uma tool call (nunca texto) - deve estourar budget
        llm = FakeLLMClient(
            [_txt("ok")] +
            [_call("validate_repo_url", {"url": f"https://dev.azure.com/x/p/_git/r{i}"}, f"t{i}") for i in range(20)]
        )
        agent = Agent(session=session, settings=settings, llm=llm, tool_budget=3)
        agent.start()
        with pytest.raises(AgentError, match="Limite de"):
            agent.send("loop")


class TestSessaoEncerrada:
    def test_send_em_sessao_done_retorna_mensagem_canonica(
        self, settings: Settings, session: SessionState
    ) -> None:
        session.phase = Phase.DONE
        llm = FakeLLMClient([])  # nao deve ser chamado
        agent = Agent(session=session, settings=settings, llm=llm)
        resp = agent.send("oi")
        assert "encerrada" in resp.lower()
