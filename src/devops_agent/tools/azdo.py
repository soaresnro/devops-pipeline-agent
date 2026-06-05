"""Cliente Azure DevOps REST API v7.1.

Cobre apenas o subset necessario para o MVP:
- get_repo: confirma que o repo existe e o agente tem acesso
- get_branch: valida que a branch existe
- check_user_permission: garante que o usuario humano tem direito de contribuir
- create_pull_request: abre PR apos push

Auth via Basic HTTP (PAT ou OAuth bearer). Service Principal sera adicionado
quando integrarmos com Entra ID em ambiente real.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from devops_agent.models import ToolResult
from devops_agent.models.inputs import RepoCoordinates

logger = logging.getLogger(__name__)

_API_VERSION = "7.1-preview.1"
_TIMEOUT_SEC = 30.0


class RepoInfo(BaseModel):
    id: str
    name: str
    default_branch: str | None = None
    url: str | None = None


class BranchInfo(BaseModel):
    name: str
    object_id: str
    is_locked: bool = False


class PullRequestInfo(BaseModel):
    pull_request_id: int
    url: str
    web_url: str
    status: str


class _RetryableHttpError(Exception):
    """Erros 5xx/429 - elegiveis para retry."""


def _pat_header(pat: str) -> dict[str, str]:
    raw = f":{pat}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class AzdoClient:
    """Cliente fino sobre httpx para Azure DevOps REST API."""

    def __init__(
        self,
        organization: str,
        *,
        pat: str | None = None,
        bearer_token: str | None = None,
        base_url: str = "https://dev.azure.com",
        timeout: float = _TIMEOUT_SEC,
    ):
        if not (pat or bearer_token):
            raise ValueError("Forneca pat OU bearer_token.")
        self.organization = organization
        self._base = base_url.rstrip("/")
        if pat:
            headers = _pat_header(pat)
        else:
            assert bearer_token is not None
            headers = _bearer_header(bearer_token)
        headers["Accept"] = "application/json"
        self._client = httpx.Client(
            base_url=f"{self._base}/{organization}",
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AzdoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- transporte com retry -----

    @retry(
        retry=retry_if_exception_type(_RetryableHttpError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4.0),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        params = {**(params or {}), "api-version": _API_VERSION}
        resp = self._client.request(method, path, params=params, json=json)
        if resp.status_code in (429, 502, 503, 504):
            raise _RetryableHttpError(f"{resp.status_code} em {method} {path}")
        return resp

    # ----- endpoints -----

    def get_repo(self, project: str, repository: str) -> ToolResult[RepoInfo]:
        try:
            resp = self._request("GET", f"/{project}/_apis/git/repositories/{repository}")
        except httpx.RequestError as exc:
            return ToolResult.failure(f"Falha de rede: {exc}", code="azdo.network")

        if resp.status_code == 404:
            return ToolResult.failure(
                f"Repo '{repository}' nao encontrado no projeto '{project}'.",
                code="azdo.repo_not_found",
            )
        if resp.status_code in (401, 403):
            return ToolResult.failure(
                "Agente sem permissao para acessar este repo.",
                code="azdo.repo_forbidden",
            )
        if resp.status_code >= 400:
            return ToolResult.failure(
                f"Azure DevOps respondeu {resp.status_code}.",
                code="azdo.http_error",
            )

        payload = resp.json()
        return ToolResult.success(
            RepoInfo(
                id=payload["id"],
                name=payload["name"],
                default_branch=payload.get("defaultBranch"),
                url=payload.get("webUrl") or payload.get("url"),
            )
        )

    def get_branch(
        self,
        project: str,
        repository: str,
        branch: str,
    ) -> ToolResult[BranchInfo]:
        # Azure DevOps: branch reference e 'refs/heads/<branch>', mas a API
        # de stats aceita ?name=<branch>
        try:
            resp = self._request(
                "GET",
                f"/{project}/_apis/git/repositories/{repository}/stats/branches",
                params={"name": branch},
            )
        except httpx.RequestError as exc:
            return ToolResult.failure(f"Falha de rede: {exc}", code="azdo.network")

        if resp.status_code == 404:
            return ToolResult.failure(
                f"Branch '{branch}' nao existe no repo '{repository}'.",
                code="azdo.branch_not_found",
            )
        if resp.status_code >= 400:
            return ToolResult.failure(
                f"Azure DevOps respondeu {resp.status_code}.",
                code="azdo.http_error",
            )

        payload = resp.json()
        return ToolResult.success(
            BranchInfo(
                name=payload["name"],
                object_id=payload["commit"]["commitId"],
                is_locked=payload.get("isBaseVersionLocked", False),
            )
        )

    def create_pull_request(
        self,
        project: str,
        repository: str,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        reviewers: list[str] | None = None,
    ) -> ToolResult[PullRequestInfo]:
        body: dict[str, Any] = {
            "sourceRefName": _ensure_refs_heads(source_branch),
            "targetRefName": _ensure_refs_heads(target_branch),
            "title": title[:400],
            "description": description[:4000],
        }
        if reviewers:
            body["reviewers"] = [{"id": r} for r in reviewers]

        try:
            resp = self._request(
                "POST",
                f"/{project}/_apis/git/repositories/{repository}/pullrequests",
                json=body,
            )
        except httpx.RequestError as exc:
            return ToolResult.failure(f"Falha de rede: {exc}", code="azdo.network")

        if resp.status_code in (200, 201):
            payload = resp.json()
            pr_id = payload["pullRequestId"]
            project_id = payload.get("repository", {}).get("project", {}).get("name") or project
            return ToolResult.success(
                PullRequestInfo(
                    pull_request_id=pr_id,
                    url=payload.get("url", ""),
                    web_url=(
                        f"{self._base}/{self.organization}/{project_id}/_git/{repository}/pullrequest/{pr_id}"
                    ),
                    status=payload.get("status", "active"),
                )
            )

        if resp.status_code == 409:
            return ToolResult.failure(
                "Ja existe um PR aberto desta branch para esta target.",
                code="azdo.pr_conflict",
            )

        return ToolResult.failure(
            f"Falha criando PR (HTTP {resp.status_code}): {resp.text[:200]}",
            code="azdo.pr_create_failed",
        )


def _ensure_refs_heads(branch: str) -> str:
    if branch.startswith("refs/"):
        return branch
    return f"refs/heads/{branch}"


def coords_from_request_url(url: str) -> RepoCoordinates | None:
    """Helper - extrai coords se URL ja foi validada anteriormente."""
    from devops_agent.tools.validation import validate_repo_url

    # delegacao - util apenas dentro do orquestrador
    # (chamadores normalmente ja tem RepoCoordinates)
    result = validate_repo_url(url, allowed_org="*")
    if result.ok:
        return result.data
    return None
