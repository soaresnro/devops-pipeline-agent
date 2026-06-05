"""Testes do cliente Azure DevOps usando respx para mockar HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from devops_agent.tools.azdo import AzdoClient


@pytest.fixture
def client() -> AzdoClient:
    return AzdoClient(organization="contoso", pat="fake-pat")


class TestGetRepo:
    @respx.mock
    def test_repo_encontrado(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "abc-123",
                    "name": "payments-api",
                    "defaultBranch": "refs/heads/main",
                    "webUrl": "https://dev.azure.com/contoso/Platform/_git/payments-api",
                },
            )
        )
        r = client.get_repo("Platform", "payments-api")
        assert r.ok
        assert r.data is not None
        assert r.data.id == "abc-123"
        assert r.data.default_branch == "refs/heads/main"

    @respx.mock
    def test_repo_404(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/missing"
        ).mock(return_value=httpx.Response(404, json={"message": "not found"}))
        r = client.get_repo("Platform", "missing")
        assert not r.ok
        assert r.error_code == "azdo.repo_not_found"

    @respx.mock
    def test_repo_forbidden(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/forbidden"
        ).mock(return_value=httpx.Response(403))
        r = client.get_repo("Platform", "forbidden")
        assert not r.ok
        assert r.error_code == "azdo.repo_forbidden"


class TestGetBranch:
    @respx.mock
    def test_branch_existe(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/stats/branches"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "main",
                    "commit": {"commitId": "deadbeef" * 5},
                    "isBaseVersionLocked": False,
                },
            )
        )
        r = client.get_branch("Platform", "payments-api", "main")
        assert r.ok
        assert r.data is not None
        assert r.data.name == "main"
        assert r.data.object_id.startswith("deadbeef")

    @respx.mock
    def test_branch_nao_existe(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/stats/branches"
        ).mock(return_value=httpx.Response(404))
        r = client.get_branch("Platform", "payments-api", "ghost")
        assert not r.ok
        assert r.error_code == "azdo.branch_not_found"


class TestListRootItems:
    @respx.mock
    def test_lista_arquivos_filtrando_pastas(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/items"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "count": 4,
                    "value": [
                        {"path": "/", "isFolder": True},
                        {"path": "/src", "isFolder": True},
                        {"path": "/go.mod", "isFolder": False},
                        {"path": "/src/main.go", "isFolder": False},
                    ],
                },
            )
        )
        r = client.list_root_items("Platform", "payments-api", "main")
        assert r.ok
        assert r.data is not None
        assert "go.mod" in r.data
        assert "src/main.go" in r.data
        # pastas nao entram
        assert "src" not in r.data

    @respx.mock
    def test_404_mapeia_items_not_found(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/items"
        ).mock(return_value=httpx.Response(404))
        r = client.list_root_items("Platform", "payments-api", "ghost")
        assert not r.ok
        assert r.error_code == "azdo.items_not_found"

    @respx.mock
    def test_403_mapeia_items_forbidden(self, client: AzdoClient) -> None:
        respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/items"
        ).mock(return_value=httpx.Response(403))
        r = client.list_root_items("Platform", "payments-api", "main")
        assert not r.ok
        assert r.error_code == "azdo.items_forbidden"


class TestCreatePullRequest:
    @respx.mock
    def test_cria_pr_com_sucesso(self, client: AzdoClient) -> None:
        respx.post(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/pullrequests"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "pullRequestId": 142,
                    "url": "https://dev.azure.com/contoso/Platform/_apis/.../pullrequests/142",
                    "status": "active",
                    "repository": {"project": {"name": "Platform"}},
                },
            )
        )
        r = client.create_pull_request(
            "Platform",
            "payments-api",
            source_branch="scaffold/api-1",
            target_branch="main",
            title="chore(ci): scaffold pipeline",
            description="Generated by agent on behalf of maria@contoso",
        )
        assert r.ok
        assert r.data is not None
        assert r.data.pull_request_id == 142
        assert "/pullrequest/142" in r.data.web_url

    @respx.mock
    def test_pr_existente_retorna_conflict(self, client: AzdoClient) -> None:
        respx.post(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/pullrequests"
        ).mock(return_value=httpx.Response(409))
        r = client.create_pull_request(
            "Platform",
            "payments-api",
            source_branch="scaffold/api-1",
            target_branch="main",
            title="t",
            description="d",
        )
        assert not r.ok
        assert r.error_code == "azdo.pr_conflict"

    @respx.mock
    def test_normaliza_target_branch_para_refs_heads(self, client: AzdoClient) -> None:
        captured_body: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured_body.update(_json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "pullRequestId": 1,
                    "url": "x",
                    "status": "active",
                    "repository": {"project": {"name": "Platform"}},
                },
            )

        respx.post(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api/pullrequests"
        ).mock(side_effect=handler)

        client.create_pull_request(
            "Platform",
            "payments-api",
            source_branch="scaffold/x",
            target_branch="main",
            title="t",
            description="d",
        )
        assert captured_body["sourceRefName"] == "refs/heads/scaffold/x"
        assert captured_body["targetRefName"] == "refs/heads/main"


class TestRetry:
    @respx.mock
    def test_retry_em_503(self, client: AzdoClient) -> None:
        route = respx.get(
            "https://dev.azure.com/contoso/Platform/_apis/git/repositories/payments-api"
        ).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(
                    200,
                    json={
                        "id": "abc",
                        "name": "payments-api",
                    },
                ),
            ]
        )
        r = client.get_repo("Platform", "payments-api")
        assert r.ok
        assert route.call_count == 3
