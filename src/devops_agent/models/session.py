"""Estado e fases da sessao conversacional."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from devops_agent.models.inputs import AppType, Language, RepoCoordinates


class Phase(StrEnum):
    GREET = "greet"
    ASK_REPO = "ask_repo"
    ASK_BRANCH = "ask_branch"
    ASK_NAMESPACE = "ask_namespace"
    ASK_APP_TYPE = "ask_app_type"
    ASK_SCHEDULE = "ask_schedule"
    DETECT_LANGUAGE = "detect_language"
    ASK_LANGUAGE = "ask_language"
    ASK_DOTNET_VERSION = "ask_dotnet_version"
    ASK_DOTNET_ENTRYPOINT = "ask_dotnet_entrypoint"
    ASK_APPLICATION_NAME = "ask_application_name"
    ASK_APIM_PATH = "ask_apim_path"
    ASK_INGRESS_HOST = "ask_ingress_host"
    VALIDATE = "validate"
    CONFIRM = "confirm"
    EXECUTE = "execute"
    DONE = "done"
    ABORTED = "aborted"
    ERROR = "error"


class ExecutionResult(BaseModel):
    """Resultado do EXECUTE - link do PR aberto no repo da aplicacao."""

    app_pr_url: str | None = None
    app_branch: str | None = None


class SessionState(BaseModel):
    """Estado completo de uma sessao do agente."""

    session_id: str = Field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:12]}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: Phase = Phase.GREET

    requested_by: str | None = None
    # Token de assercao do usuario para o fluxo OBO (modo sp). Sensivel: nunca logar.
    user_assertion: str | None = Field(default=None, repr=False, exclude=True)
    user_oid: str | None = None
    repo_url: str | None = None
    repo_coords: RepoCoordinates | None = None
    branch: str | None = None
    namespace: str | None = None
    app_type: AppType | None = None
    app_name: str | None = None
    language: Language | None = None
    language_candidates: list[Language] = Field(default_factory=list)
    schedule: str | None = None  # cron do CronJob; None usa o default do scaffold
    dotnet_version: str | None = None  # versao .NET; None usa o default do scaffold
    dotnet_entrypoint: str | None = None  # assembly de entrada; None usa app_name.dll
    application_name: str | None = None  # applicationName da pipeline; None usa app_name
    apim_path: str | None = None  # pathApiManagementService; None deriva de application_name
    ingress_host: str | None = None  # host base do ingress; None deriva de apim_path

    confirmed: bool = False
    execution: ExecutionResult | None = None

    errors: list[str] = Field(default_factory=list)
    audit: list[dict[str, Any]] = Field(default_factory=list)

    def record_audit(self, event: str, **payload: Any) -> None:
        self.audit.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "event": event,
                **payload,
            }
        )
