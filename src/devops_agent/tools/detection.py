"""Deteccao de linguagem a partir dos arquivos do repositorio da aplicacao.

Funcao PURA: recebe a lista de paths do repo (obtida via Azure DevOps Items API,
sem clone) e infere a linguagem por marcadores conhecidos. Quando 0 ou >1
linguagens batem, o resultado e inconclusivo e o orquestrador pergunta ao usuario.

A escolha da linguagem afeta apenas o Dockerfile gerado - o Helm chart e a
pipeline continuam funcao apenas do app_type.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import PurePosixPath

from pydantic import BaseModel

from devops_agent.models import Language, ToolResult

# Marcadores por linguagem. Casados contra o basename (minusculo) de cada path.
# Padroes com curinga usam fnmatch; o resto e igualdade exata.
_LANGUAGE_MARKERS: dict[Language, tuple[str, ...]] = {
    Language.DOTNET: ("*.csproj", "*.sln", "*.fsproj", "global.json"),
    Language.NODE: ("package.json",),
    Language.PYTHON: ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
    Language.GO: ("go.mod",),
}


class LanguageDetection(BaseModel):
    """Resultado da deteccao de linguagem."""

    language: Language | None = None
    candidates: list[Language] = []
    is_conclusive: bool = False


def _matches(basename: str, pattern: str) -> bool:
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return fnmatch(basename, pattern)
    return basename == pattern


def detect_language(paths: Iterable[str]) -> ToolResult[LanguageDetection]:
    """Infere a linguagem a partir dos paths do repo.

    Conclusivo apenas quando exatamente uma linguagem tem marcadores presentes.
    """
    basenames = {PurePosixPath(p).name.lower() for p in paths if p}

    matched: list[Language] = [
        lang
        for lang, patterns in _LANGUAGE_MARKERS.items()
        if any(_matches(name, pat) for name in basenames for pat in patterns)
    ]

    if len(matched) == 1:
        return ToolResult.success(
            LanguageDetection(language=matched[0], candidates=matched, is_conclusive=True)
        )

    return ToolResult.success(
        LanguageDetection(language=None, candidates=matched, is_conclusive=False)
    )
