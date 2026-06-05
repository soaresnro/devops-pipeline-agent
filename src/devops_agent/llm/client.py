"""Abstracao sobre o LLM provider.

A interface `LLMClient` isola o resto do codigo do SDK especifico (Azure OpenAI).
Trocar para Bedrock/Vertex no futuro = uma classe nova implementando o protocolo.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class ToolCall(BaseModel):
    """Uma chamada de tool emitida pelo LLM."""

    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ToolCall":
        """Constroi a partir do formato OpenAI tool_call (dict)."""
        fn = raw.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
        except json.JSONDecodeError:
            args = {}
        return cls(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args)


class Completion(BaseModel):
    """Resposta do LLM."""

    text: str = ""
    tool_calls: list[ToolCall] = []
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMClient(Protocol):
    """Interface minima esperada pelo agent loop."""

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> Completion:
        ...


class AzureOpenAIClient:
    """Implementacao concreta para Azure OpenAI via SDK `openai`."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-10-21",
    ):
        try:
            from openai import AzureOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Pacote 'openai' nao instalado. Instale com: pip install -e \".[llm]\""
            ) from exc

        self._deployment = deployment
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self._deployment,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            tool_calls.append(
                ToolCall.from_raw(
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
            )

        return Completion(
            text=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
        )


class FakeLLMClient:
    """Implementacao stub para testes: replays respostas pre-programadas.

    Cada chamada de `complete` consome o proximo `Completion` da fila.
    Permite simular fluxos complexos (multi-turn + tool calls) sem rede.
    """

    def __init__(self, scripted_responses: list[Completion]) -> None:
        self._queue = list(scripted_responses)
        self.received_messages: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> Completion:
        self.received_messages.append(list(messages))
        if not self._queue:
            raise RuntimeError(
                "FakeLLMClient ficou sem respostas - script incompleto para o cenario."
            )
        return self._queue.pop(0)

    @property
    def calls_remaining(self) -> int:
        return len(self._queue)


def assistant_message(completion: Completion) -> dict[str, Any]:
    """Converte um Completion em uma assistant message no formato OpenAI."""
    msg: dict[str, Any] = {"role": "assistant", "content": completion.text or None}
    if completion.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in completion.tool_calls
        ]
    return msg


def tool_result_message(tool_call_id: str, result_payload: dict[str, Any]) -> dict[str, Any]:
    """Mensagem `role=tool` devolvendo o resultado de uma tool call ao LLM."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result_payload, ensure_ascii=False, default=str),
    }
