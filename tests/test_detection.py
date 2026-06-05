"""Testes da deteccao de linguagem (funcao pura sobre paths do repo)."""

from __future__ import annotations

import pytest

from devops_agent.models import Language
from devops_agent.tools.detection import detect_language


class TestDetectLanguageConclusivo:
    @pytest.mark.parametrize(
        ("paths", "esperado"),
        [
            (["src/Api/Api.csproj", "README.md"], Language.DOTNET),
            (["Service.sln", "src/x.cs"], Language.DOTNET),
            (["global.json", "Program.cs"], Language.DOTNET),
            (["package.json", "src/index.ts"], Language.NODE),
            (["pyproject.toml", "app/main.py"], Language.PYTHON),
            (["requirements.txt", "app.py"], Language.PYTHON),
            (["go.mod", "main.go"], Language.GO),
        ],
    )
    def test_um_marcador_define_a_linguagem(
        self, paths: list[str], esperado: Language
    ) -> None:
        r = detect_language(paths)
        assert r.ok
        assert r.data is not None
        assert r.data.is_conclusive is True
        assert r.data.language == esperado

    def test_csproj_em_subpasta_e_detectado(self) -> None:
        r = detect_language(["/src/Worker/Worker.csproj", "/Dockerfile"])
        assert r.data is not None
        assert r.data.language == Language.DOTNET


class TestDetectLanguageInconclusivo:
    def test_sem_marcadores_e_inconclusivo_sem_candidatos(self) -> None:
        r = detect_language(["README.md", "LICENSE", "docs/guide.md"])
        assert r.ok
        assert r.data is not None
        assert r.data.is_conclusive is False
        assert r.data.language is None
        assert r.data.candidates == []

    def test_multiplas_linguagens_lista_candidatos(self) -> None:
        r = detect_language(["package.json", "go.mod", "requirements.txt"])
        assert r.data is not None
        assert r.data.is_conclusive is False
        assert r.data.language is None
        assert set(r.data.candidates) == {Language.NODE, Language.GO, Language.PYTHON}

    def test_lista_vazia_e_inconclusivo(self) -> None:
        r = detect_language([])
        assert r.data is not None
        assert r.data.is_conclusive is False
