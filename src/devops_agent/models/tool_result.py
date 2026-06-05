"""Contrato unificado de retorno das tools."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    """Resultado padronizado de uma tool.

    O LLM e o orquestrador inspecionam `ok` para decidir o proximo passo;
    `error` carrega mensagem amigavel (ja traduzida para o usuario).
    """

    ok: bool
    data: T | None = None
    error: str | None = None
    error_code: str | None = None

    @classmethod
    def success(cls, data: T) -> "ToolResult[T]":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error: str, code: str | None = None) -> "ToolResult[T]":
        return cls(ok=False, error=error, error_code=code)
