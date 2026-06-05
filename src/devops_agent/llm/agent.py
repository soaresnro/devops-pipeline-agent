"""Agent loop conversacional.

Mantem o historico de mensagens, chama o LLM com as tools registradas, executa
as tool calls via dispatcher, e devolve a mensagem textual final ao usuario.

A maquina de estados (Phase) e mantida no SessionState - o agent loop nao toma
decisoes sobre fase; apenas garante o protocolo conversacional e o roteamento.
"""

from __future__ import annotations

import logging
from typing import Any

from devops_agent.config import Settings
from devops_agent.llm.client import (
    Completion,
    LLMClient,
    assistant_message,
    tool_result_message,
)
from devops_agent.llm.dispatcher import dispatch
from devops_agent.llm.schemas import TOOL_NAMES, tools_for_agent
from devops_agent.llm.system_prompt import SYSTEM_PROMPT, wrap_user_input
from devops_agent.models import Phase, SessionState

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Falha do agent loop nao recuperavel (ex.: limite de iteracoes)."""


class Agent:
    """Agente conversacional single-session."""

    DEFAULT_TOOL_BUDGET = 12

    def __init__(
        self,
        *,
        session: SessionState,
        settings: Settings,
        llm: LLMClient,
        tool_budget: int = DEFAULT_TOOL_BUDGET,
        temperature: float = 0.2,
    ):
        self.session = session
        self.settings = settings
        self.llm = llm
        self.tool_budget = tool_budget
        self.temperature = temperature
        self._tools = tools_for_agent()
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def start(self) -> str:
        """Inicia a sessao: seed implicito para o LLM fazer a primeira pergunta."""
        self._messages.append(
            {
                "role": "user",
                "content": (
                    "<session_start>O usuario abriu uma nova sessao para criar uma "
                    "pipeline. Cumprimente-o brevemente e faca a primeira pergunta."
                    "</session_start>"
                ),
            }
        )
        return self._run_until_text()

    def send(self, user_text: str) -> str:
        """Envia uma mensagem do usuario e retorna a resposta textual do agente."""
        if self.session.phase in {Phase.DONE, Phase.ABORTED, Phase.ERROR}:
            return "Esta sessao ja foi encerrada. Inicie uma nova para criar outra pipeline."

        self._messages.append({"role": "user", "content": wrap_user_input(user_text)})
        return self._run_until_text()

    def _run_until_text(self) -> str:
        """Loop principal: chama LLM e despacha tools ate ter texto para o usuario."""
        for _iteration in range(self.tool_budget):
            completion = self.llm.complete(
                self._messages,
                self._tools,
                temperature=self.temperature,
            )
            self._messages.append(assistant_message(completion))

            if not completion.has_tool_calls:
                return completion.text or ""

            # Executa cada tool call e responde ao LLM
            for tc in completion.tool_calls:
                if tc.name not in TOOL_NAMES:
                    payload = {
                        "ok": False,
                        "error": f"tool '{tc.name}' nao existe.",
                        "error_code": "tool.unknown",
                    }
                else:
                    payload = dispatch(tc, self.session, self.settings)

                self._messages.append(tool_result_message(tc.id, payload))
                logger.info(
                    "tool_call name=%s ok=%s phase=%s",
                    tc.name,
                    payload.get("ok"),
                    self.session.phase.value,
                )

        raise AgentError(
            f"Limite de {self.tool_budget} iteracoes de tool atingido sem resposta final."
        )

    def is_finished(self) -> bool:
        return self.session.phase in {Phase.DONE, Phase.ABORTED, Phase.ERROR}
