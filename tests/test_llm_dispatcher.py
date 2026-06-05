"""Testes do dispatcher de tools - verifica side effects e robustez."""

from __future__ import annotations

import pytest

from devops_agent.config import Settings
from devops_agent.llm.client import ToolCall
from devops_agent.llm.dispatcher import dispatch
from devops_agent.models import AppType, Phase, SessionState


@pytest.fixture
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        azdo_org="contoso",
        devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState(requested_by="tester@local")


class TestValidateRepoTool:
    def test_atualiza_sessao_em_sucesso(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(
            id="t1",
            name="validate_repo_url",
            arguments={"url": "https://dev.azure.com/contoso/Platform/_git/payments-api"},
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert out["data"]["repository"] == "payments-api"
        assert session.repo_coords is not None
        assert session.app_name == "payments-api"
        assert session.phase == Phase.ASK_BRANCH

    def test_falha_em_org_errada(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(
            id="t1",
            name="validate_repo_url",
            arguments={"url": "https://dev.azure.com/outra/Platform/_git/x"},
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is False
        assert out["error_code"] == "repo.org_not_allowed"
        assert session.repo_coords is None  # nao modifica sessao em falha

    def test_args_invalidos(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(id="t1", name="validate_repo_url", arguments={})
        out = dispatch(call, session, settings)
        assert out["ok"] is False
        assert out["error_code"] == "args.invalid"


class TestValidateBranchTool:
    def test_ok_avanca_para_namespace(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(id="t1", name="validate_branch", arguments={"branch": "main"})
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert session.branch == "main"
        assert session.phase == Phase.ASK_NAMESPACE


class TestValidateNamespaceTool:
    def test_ok_avanca_para_tipo(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(
            id="t1", name="validate_namespace", arguments={"namespace": "payments-prod"}
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert session.namespace == "payments-prod"
        assert session.phase == Phase.ASK_APP_TYPE

    def test_namespace_reservado(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(
            id="t1", name="validate_namespace", arguments={"namespace": "kube-system"}
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is False
        assert out["error_code"] == "ns.reserved"


class TestValidateAppTypeTool:
    def test_aceita_api(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(id="t1", name="validate_app_type", arguments={"value": "api"})
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert session.app_type == AppType.API
        assert session.phase == Phase.VALIDATE


class TestProposeScaffold:
    def test_gera_resumo_com_inputs_completos(
        self, settings: Settings, session: SessionState
    ) -> None:
        # Setup: simula que as 4 validacoes ja rodaram
        from devops_agent.models.inputs import RepoCoordinates

        session.repo_coords = RepoCoordinates(
            organization="contoso", project="Platform", repository="payments-api"
        )
        session.app_name = "payments-api"
        session.branch = "main"
        session.namespace = "payments-prod"
        session.app_type = AppType.API
        session.phase = Phase.VALIDATE

        call = ToolCall(id="t1", name="propose_scaffold", arguments={})
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert "Dockerfile" in out["data"]["app_repo_files"]
        assert "azure-pipelines.yaml" in out["data"]["app_repo_files"]
        assert any("Chart.yaml" in p for p in out["data"]["helm_chart_files"])
        assert session.phase == Phase.CONFIRM

    def test_falha_com_inputs_faltando(
        self, settings: Settings, session: SessionState
    ) -> None:
        call = ToolCall(id="t1", name="propose_scaffold", arguments={})
        out = dispatch(call, session, settings)
        assert out["ok"] is False


class TestExecuteScaffoldGuards:
    def test_recusa_se_fase_nao_eh_confirm(
        self, settings: Settings, session: SessionState
    ) -> None:
        session.phase = Phase.ASK_REPO  # nao chegou no CONFIRM
        call = ToolCall(
            id="t1",
            name="execute_scaffold",
            arguments={"user_confirmation": "sim"},
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is False
        assert out["error_code"] == "execute.wrong_phase"


class TestAbortSession:
    def test_marca_sessao_como_aborted(
        self, settings: Settings, session: SessionState
    ) -> None:
        call = ToolCall(
            id="t1", name="abort_session", arguments={"reason": "usuario_recusou"}
        )
        out = dispatch(call, session, settings)
        assert out["ok"] is True
        assert session.phase == Phase.ABORTED
        # audit registrado
        assert any(e["event"] == "session_aborted" for e in session.audit)


class TestUnknownTool:
    def test_retorna_failure(self, settings: Settings, session: SessionState) -> None:
        call = ToolCall(id="t1", name="rm_rf", arguments={})
        out = dispatch(call, session, settings)
        assert out["ok"] is False
        assert out["error_code"] == "tool.unknown"
