"""Testes das transicoes de fase da state machine (foco na deteccao de linguagem)."""

from __future__ import annotations

import pytest

from devops_agent.models import AppType, Language, Phase, SessionState
from devops_agent.orchestrator import state_machine as sm


@pytest.fixture
def session() -> SessionState:
    s = SessionState(requested_by="tester@local")
    s.phase = Phase.ASK_APP_TYPE
    return s


class TestHandleAppType:
    def test_tipo_processo_vai_para_detect_language(self, session: SessionState) -> None:
        step = sm.handle_app_type(session, "api")
        assert not step.error
        assert session.app_type == AppType.API
        assert session.phase == Phase.DETECT_LANGUAGE
        assert step.next_phase == Phase.DETECT_LANGUAGE

    def test_web_pula_deteccao_e_vai_para_ask_ingress_host(self, session: SessionState) -> None:
        # web usa o microservice-chart (com ingresses), entao pergunta o host antes de validar
        step = sm.handle_app_type(session, "web")
        assert not step.error
        assert session.app_type == AppType.WEB
        assert session.phase == Phase.ASK_INGRESS_HOST
        assert session.language is None

    def test_cronjob_vai_para_ask_schedule(self, session: SessionState) -> None:
        step = sm.handle_app_type(session, "cronjob")
        assert not step.error
        assert session.app_type == AppType.CRONJOB
        assert session.phase == Phase.ASK_SCHEDULE

    def test_tipo_invalido_permanece_em_ask_app_type(self, session: SessionState) -> None:
        step = sm.handle_app_type(session, "lambda")
        assert step.error
        assert step.next_phase == Phase.ASK_APP_TYPE


class TestHandleLanguage:
    def test_linguagem_valida_vai_para_validate(self, session: SessionState) -> None:
        session.phase = Phase.ASK_LANGUAGE
        step = sm.handle_language(session, "python")
        assert not step.error
        assert session.language == Language.PYTHON
        assert session.phase == Phase.VALIDATE

    def test_alias_aceito(self, session: SessionState) -> None:
        session.phase = Phase.ASK_LANGUAGE
        step = sm.handle_language(session, "golang")
        assert not step.error
        assert session.language == Language.GO

    def test_dotnet_vai_para_ask_dotnet_version(self, session: SessionState) -> None:
        session.phase = Phase.ASK_LANGUAGE
        step = sm.handle_language(session, "dotnet")
        assert not step.error
        assert session.language == Language.DOTNET
        assert session.phase == Phase.ASK_DOTNET_VERSION

    def test_invalida_permanece_em_ask_language(self, session: SessionState) -> None:
        session.phase = Phase.ASK_LANGUAGE
        step = sm.handle_language(session, "cobol")
        assert step.error
        assert step.next_phase == Phase.ASK_LANGUAGE
        # nao volta para DETECT_LANGUAGE (evita re-bater na API)
        assert session.phase == Phase.ASK_LANGUAGE
        assert session.language is None


class TestHandleSchedule:
    def test_cron_valido_vai_para_detect_language(self, session: SessionState) -> None:
        session.phase = Phase.ASK_SCHEDULE
        step = sm.handle_schedule(session, "0 2 * * *")
        assert not step.error
        assert session.schedule == "0 2 * * *"
        assert session.phase == Phase.DETECT_LANGUAGE

    def test_vazio_mantem_default_e_avanca(self, session: SessionState) -> None:
        session.phase = Phase.ASK_SCHEDULE
        step = sm.handle_schedule(session, "   ")
        assert not step.error
        assert session.schedule is None  # default aplicado no scaffold
        assert session.phase == Phase.DETECT_LANGUAGE

    def test_invalido_permanece_em_ask_schedule(self, session: SessionState) -> None:
        session.phase = Phase.ASK_SCHEDULE
        step = sm.handle_schedule(session, "0 2 *")
        assert step.error
        assert step.next_phase == Phase.ASK_SCHEDULE
        assert session.schedule is None


class TestHandleDotnetVersion:
    def test_versao_valida_vai_para_entrypoint(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_VERSION
        step = sm.handle_dotnet_version(session, "9.0.x")
        assert not step.error
        assert session.dotnet_version == "9.0.x"
        assert session.phase == Phase.ASK_DOTNET_ENTRYPOINT

    def test_vazio_mantem_default_e_avanca(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_VERSION
        step = sm.handle_dotnet_version(session, "  ")
        assert not step.error
        assert session.dotnet_version is None  # default aplicado no scaffold
        assert session.phase == Phase.ASK_DOTNET_ENTRYPOINT

    def test_invalida_permanece_em_ask_dotnet_version(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_VERSION
        step = sm.handle_dotnet_version(session, "10.1.2")
        assert step.error
        assert step.next_phase == Phase.ASK_DOTNET_VERSION
        assert session.dotnet_version is None


class TestHandleDotnetEntrypoint:
    def test_entrypoint_valido_vai_para_ask_application_name(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_ENTRYPOINT
        step = sm.handle_dotnet_entrypoint(session, "ePharma.Auth.Services")
        assert not step.error
        assert session.dotnet_entrypoint == "ePharma.Auth.Services.dll"  # .dll adicionado
        assert session.phase == Phase.ASK_APPLICATION_NAME

    def test_vazio_mantem_default_e_pergunta_application_name(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_ENTRYPOINT
        step = sm.handle_dotnet_entrypoint(session, "")
        assert not step.error
        assert session.dotnet_entrypoint is None  # default app_name.dll no scaffold
        assert session.phase == Phase.ASK_APPLICATION_NAME

    def test_invalido_permanece(self, session: SessionState) -> None:
        session.phase = Phase.ASK_DOTNET_ENTRYPOINT
        step = sm.handle_dotnet_entrypoint(session, "nome invalido!")
        assert step.error
        assert step.next_phase == Phase.ASK_DOTNET_ENTRYPOINT
        assert session.dotnet_entrypoint is None


class TestHandleApplicationName:
    def test_valido_api_vai_para_apim_path(self, session: SessionState) -> None:
        session.app_type = AppType.API
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "api-utils")
        assert not step.error
        assert session.application_name == "api-utils"
        assert session.phase == Phase.ASK_APIM_PATH

    def test_vazio_usa_app_name(self, session: SessionState) -> None:
        session.app_type = AppType.API
        session.app_name = "epharma-api-utils"
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "")
        assert not step.error
        assert session.application_name == "epharma-api-utils"
        assert session.phase == Phase.ASK_APIM_PATH

    def test_worker_pula_apim_e_vai_para_validate(self, session: SessionState) -> None:
        session.app_type = AppType.WORKER
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "billing-worker")
        assert not step.error
        assert session.phase == Phase.VALIDATE

    def test_invalido_permanece(self, session: SessionState) -> None:
        session.app_type = AppType.API
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "Nome Invalido!")
        assert step.error
        assert step.next_phase == Phase.ASK_APPLICATION_NAME
        assert session.application_name is None


class TestHandleApimPath:
    def test_valido_vai_para_ingress_host(self, session: SessionState) -> None:
        session.application_name = "api-utils"
        session.phase = Phase.ASK_APIM_PATH
        step = sm.handle_apim_path(session, "utils")
        assert not step.error
        assert session.apim_path == "utils"
        assert session.phase == Phase.ASK_INGRESS_HOST

    def test_vazio_deriva_do_application_name(self, session: SessionState) -> None:
        session.application_name = "api-utils"
        session.phase = Phase.ASK_APIM_PATH
        step = sm.handle_apim_path(session, "")
        assert not step.error
        assert session.apim_path == "utils"  # remove o prefixo 'api-'
        assert session.phase == Phase.ASK_INGRESS_HOST


class TestHandleIngressHost:
    def test_valido_vai_para_validate(self, session: SessionState) -> None:
        session.apim_path = "utils"
        session.phase = Phase.ASK_INGRESS_HOST
        step = sm.handle_ingress_host(session, "servicesutils")
        assert not step.error
        assert session.ingress_host == "servicesutils"
        assert session.phase == Phase.VALIDATE

    def test_vazio_deriva_de_services_path(self, session: SessionState) -> None:
        session.apim_path = "utils"
        session.phase = Phase.ASK_INGRESS_HOST
        step = sm.handle_ingress_host(session, "")
        assert not step.error
        assert session.ingress_host == "servicesutils"
        assert session.phase == Phase.VALIDATE

    def test_invalido_permanece(self, session: SessionState) -> None:
        session.apim_path = "utils"
        session.phase = Phase.ASK_INGRESS_HOST
        step = sm.handle_ingress_host(session, "host.com.invalido")
        assert step.error
        assert step.next_phase == Phase.ASK_INGRESS_HOST
        assert session.ingress_host is None
