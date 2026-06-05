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

    def test_web_pula_deteccao_e_vai_para_validate(self, session: SessionState) -> None:
        step = sm.handle_app_type(session, "web")
        assert not step.error
        assert session.app_type == AppType.WEB
        assert session.phase == Phase.VALIDATE
        assert session.language is None

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

    def test_invalida_permanece_em_ask_language(self, session: SessionState) -> None:
        session.phase = Phase.ASK_LANGUAGE
        step = sm.handle_language(session, "cobol")
        assert step.error
        assert step.next_phase == Phase.ASK_LANGUAGE
        # nao volta para DETECT_LANGUAGE (evita re-bater na API)
        assert session.phase == Phase.ASK_LANGUAGE
        assert session.language is None
