"""Modelos de entrada do usuario."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class AppType(StrEnum):
    API = "api"
    WORKER = "worker"
    WEB = "web"


class RepoCoordinates(BaseModel):
    """Coordenadas normalizadas de um repositorio Azure DevOps."""

    organization: str
    project: str
    repository: str

    @property
    def full_name(self) -> str:
        return f"{self.organization}/{self.project}/{self.repository}"

    def to_clone_url(self) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_git/{self.repository}"


class PipelineRequest(BaseModel):
    """Inputs coletados do usuario nas 4 perguntas."""

    repo_url: HttpUrl
    branch: str = Field(..., min_length=1, max_length=200)
    namespace: str = Field(..., min_length=1, max_length=63)
    app_type: AppType
    requested_by: str | None = Field(
        default=None,
        description="Identificador do usuario humano (email/oid) para auditoria.",
    )

    @property
    def app_name(self) -> str:
        """Inferido do repo - sera setado apos parse da URL."""
        return getattr(self, "_app_name", "")
