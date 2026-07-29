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

    def test_remove_prefixo_igual_ao_namespace(self, session: SessionState) -> None:
        # ns=autorizador + nome=autorizador-api-teste -> api-teste (evita sa-<ns>-<ns>-...)
        session.app_type = AppType.API
        session.namespace = "autorizador"
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "autorizador-api-teste")
        assert not step.error
        assert session.application_name == "api-teste"

    def test_vazio_com_repo_prefixado_pelo_namespace(self, session: SessionState) -> None:
        # default (app_name) tambem tem o prefixo do namespace removido
        session.app_type = AppType.API
        session.namespace = "autorizador"
        session.app_name = "autorizador-api-teste"
        session.phase = Phase.ASK_APPLICATION_NAME
        step = sm.handle_application_name(session, "")
        assert not step.error
        assert session.application_name == "api-teste"


class TestHandleApimPath:
    def test_valido_vai_para_api_ids(self, session: SessionState) -> None:
        session.application_name = "api-utils"
        session.phase = Phase.ASK_APIM_PATH
        step = sm.handle_apim_path(session, "utils")
        assert not step.error
        assert session.apim_path == "utils"
        assert session.phase == Phase.ASK_API_IDS

    def test_vazio_deriva_do_application_name(self, session: SessionState) -> None:
        session.application_name = "api-utils"
        session.phase = Phase.ASK_APIM_PATH
        step = sm.handle_apim_path(session, "")
        assert not step.error
        assert session.apim_path == "utils"  # remove o prefixo 'api-'
        assert session.phase == Phase.ASK_API_IDS


class TestHandleApiIds:
    def test_quatro_valores_mapeiam_na_ordem(self, session: SessionState) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, "utils-dev,utils-qa,utils-uat,utils")
        assert not step.error
        assert session.api_ids == {
            "dev": "utils-dev",
            "qa": "utils-qa",
            "uat": "utils-uat",
            "prod": "utils",
        }
        assert session.phase == Phase.ASK_INGRESS_HOST

    def test_valor_unico_vale_para_todos_e_eco_deixa_explicito(
        self, session: SessionState
    ) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, "api-utils")
        assert not step.error
        assert session.api_ids == dict.fromkeys(("dev", "qa", "uat", "prod"), "api-utils")
        # o eco confirma ao usuario que o mesmo valor cobre os quatro ambientes
        assert "o mesmo para" in step.message_to_user
        assert "api-utils" in step.message_to_user

    def test_espacos_em_volta_das_virgulas_sao_ignorados(self, session: SessionState) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, " utils-dev , utils-qa , utils-uat , utils ")
        assert not step.error
        assert session.api_ids is not None
        assert session.api_ids["dev"] == "utils-dev"
        assert session.api_ids["prod"] == "utils"

    def test_vazio_e_obrigatorio_permanece(self, session: SessionState) -> None:
        # apiIds nao tem default: entrada vazia re-pergunta
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, "   ")
        assert step.error
        assert step.next_phase == Phase.ASK_API_IDS
        assert session.api_ids is None

    @pytest.mark.parametrize("entrada", ["a,b", "a,b,c", "a,b,c,d,e"])
    def test_quantidade_diferente_de_1_ou_4_repergunta(
        self, entrada: str, session: SessionState
    ) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, entrada)
        assert step.error
        assert step.next_phase == Phase.ASK_API_IDS
        assert session.api_ids is None

    def test_posicao_vazia_entre_virgulas_repergunta(self, session: SessionState) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, "utils-dev,,utils-uat,utils")
        assert step.error
        assert "qa" in (step.error or "")
        assert session.api_ids is None

    def test_valor_invalido_aponta_o_ambiente(self, session: SessionState) -> None:
        session.phase = Phase.ASK_API_IDS
        step = sm.handle_api_ids(session, "utils-dev,utils-qa,utils uat,utils")
        assert step.error
        assert "uat" in (step.error or "")
        assert session.api_ids is None


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
