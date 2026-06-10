"""Testes do executor com foco no modo 'sp': OBO fail-closed e abertura de PR.

As operacoes de git sao mockadas (monkeypatch no namespace do executor) para
isolar a orquestracao; chamadas HTTP do AzdoClient sao mockadas via respx.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from devops_agent.config import Settings
from devops_agent.models import AppType, Language, Phase, SessionState, ToolResult
from devops_agent.models.inputs import RepoCoordinates
from devops_agent.orchestrator import executor as exec_mod

_ORG = "https://dev.azure.com/contoso"
_PERM_URL = f"{_ORG}/_apis/security/permissionevaluationbatch"
_REPO_URL = f"{_ORG}/Platform/_apis/git/repositories/payments-api"
_ITEMS_URL = f"{_ORG}/Platform/_apis/git/repositories/payments-api/items"


@pytest.fixture
def sp_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        azdo_org="contoso",
        azdo_auth_mode="sp",
        azdo_sp_tenant_id="tenant",
        azdo_sp_client_id="client",
        azdo_sp_client_secret="secret",
        devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
    )


@pytest.fixture
def confirmed_session() -> SessionState:
    s = SessionState(requested_by="maria@contoso.com", user_oid="oid-123")
    s.repo_coords = RepoCoordinates(
        organization="contoso", project="Platform", repository="payments-api"
    )
    s.app_name = "payments-api"
    s.branch = "main"
    s.namespace = "payments-prod"
    s.app_type = AppType.API
    s.confirmed = True
    return s


def _mock_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        exec_mod, "acquire_app_token", lambda settings: ToolResult.success("app-token")
    )
    monkeypatch.setattr(
        exec_mod,
        "acquire_user_obo_token",
        lambda settings, assertion: ToolResult.success("obo-token"),
    )


def _mock_git_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faz todas as operacoes de git terem sucesso sem tocar em disco/rede."""
    monkeypatch.setattr(
        exec_mod, "clone_shallow", lambda ws, **kw: ToolResult.success(Path(ws.root) / kw["key"])
    )
    monkeypatch.setattr(exec_mod, "list_existing_paths", lambda p: set())
    monkeypatch.setattr(exec_mod, "write_files", lambda p, files: ToolResult.success(len(files)))
    monkeypatch.setattr(
        exec_mod, "create_branch_and_commit", lambda p, **kw: ToolResult.success("a" * 40)
    )
    monkeypatch.setattr(exec_mod, "push_branch", lambda p, **kw: ToolResult.success(kw["branch"]))


def _repo_response() -> httpx.Response:
    return httpx.Response(
        200, json={"id": "repo-guid", "name": "payments-api", "project": {"id": "proj-guid"}}
    )


class TestSpFailClosed:
    def test_sem_user_assertion_recusa(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        _mock_tokens(monkeypatch)
        # confirmed_session NAO tem user_assertion
        r = exec_mod.execute(confirmed_session, sp_settings)
        assert not r.ok
        assert r.error_code == "execute.missing_user_assertion"

    @respx.mock
    def test_permissao_negada_recusa_sem_clonar(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        _mock_tokens(monkeypatch)
        confirmed_session.user_assertion = "user-jwt"

        # Se clone for chamado, falha o teste (nao deve chegar la).
        def _no_clone(*a: object, **k: object) -> ToolResult[Path]:
            raise AssertionError("clone nao deveria ser chamado quando permissao e negada")

        monkeypatch.setattr(exec_mod, "clone_shallow", _no_clone)

        respx.get(_REPO_URL).mock(return_value=_repo_response())
        respx.post(_PERM_URL).mock(
            return_value=httpx.Response(200, json={"evaluations": [{"value": False}]})
        )

        r = exec_mod.execute(confirmed_session, sp_settings)
        assert not r.ok
        assert r.error_code == "execute.permission_denied"
        # auditoria registrada com allowed=False
        assert any(
            e["event"] == "permission_checked" and e["allowed"] is False
            for e in confirmed_session.audit
        )

    @respx.mock
    def test_consulta_de_permissao_403_recusa(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        _mock_tokens(monkeypatch)
        confirmed_session.user_assertion = "user-jwt"
        respx.get(_REPO_URL).mock(return_value=_repo_response())
        respx.post(_PERM_URL).mock(return_value=httpx.Response(403))
        r = exec_mod.execute(confirmed_session, sp_settings)
        assert not r.ok
        assert r.error_code == "azdo.permission_query_forbidden"

    def test_falha_app_token_recusa(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        monkeypatch.setattr(
            exec_mod,
            "acquire_app_token",
            lambda settings: ToolResult.failure("sem credencial", code="auth.app_token"),
        )
        confirmed_session.user_assertion = "user-jwt"
        r = exec_mod.execute(confirmed_session, sp_settings)
        assert not r.ok
        assert r.error_code == "auth.app_token"


class TestSpHappyPath:
    @respx.mock
    def test_permitido_abre_pr_com_bearer(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        _mock_tokens(monkeypatch)
        _mock_git_ok(monkeypatch)
        confirmed_session.user_assertion = "user-jwt"

        respx.get(_REPO_URL).mock(return_value=_repo_response())
        respx.post(_PERM_URL).mock(
            return_value=httpx.Response(200, json={"evaluations": [{"value": True}]})
        )

        seen_auth: list[str] = []

        def _pr_handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization", ""))
            return httpx.Response(
                201,
                json={
                    "pullRequestId": 7,
                    "url": "x",
                    "status": "active",
                    "repository": {"project": {"name": "Platform"}},
                },
            )

        respx.post(url__regex=r".*/pullrequests").mock(side_effect=_pr_handler)

        r = exec_mod.execute(confirmed_session, sp_settings)
        assert r.ok, r.error
        assert r.data is not None
        assert r.data.app_pr_url is not None
        # PRs abertos com o token do SP (bearer), nao PAT
        assert seen_auth and all(a == "Bearer app-token" for a in seen_auth)


class TestDetectLanguageStepSp:
    @respx.mock
    def test_sp_usa_app_token_e_detecta(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        monkeypatch.setattr(
            exec_mod, "acquire_app_token", lambda settings: ToolResult.success("app-token")
        )
        seen_auth: list[str] = []

        def _items(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization", ""))
            return httpx.Response(
                200, json={"value": [{"path": "/go.mod", "isFolder": False}]}
            )

        respx.get(_ITEMS_URL).mock(side_effect=_items)

        r = exec_mod.detect_language_step(confirmed_session, sp_settings)
        assert r.ok
        assert confirmed_session.language == Language.GO
        assert confirmed_session.phase == Phase.VALIDATE
        # usou o token do SP (bearer), nao PAT
        assert seen_auth == ["Bearer app-token"]

    @respx.mock
    def test_sp_detecta_dotnet_vai_para_ask_dotnet_version(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        monkeypatch.setattr(
            exec_mod, "acquire_app_token", lambda settings: ToolResult.success("app-token")
        )
        respx.get(_ITEMS_URL).mock(
            return_value=httpx.Response(
                200, json={"value": [{"path": "/Api.csproj", "isFolder": False}]}
            )
        )
        r = exec_mod.detect_language_step(confirmed_session, sp_settings)
        assert r.ok
        assert confirmed_session.language == Language.DOTNET
        assert confirmed_session.phase == Phase.ASK_DOTNET_VERSION

    def test_sp_sem_app_token_cai_em_ask_language(
        self, monkeypatch: pytest.MonkeyPatch, sp_settings: Settings, confirmed_session: SessionState
    ) -> None:
        monkeypatch.setattr(
            exec_mod,
            "acquire_app_token",
            lambda settings: ToolResult.failure("sem credencial", code="auth.app_token"),
        )
        r = exec_mod.detect_language_step(confirmed_session, sp_settings)
        assert r.ok
        assert r.data is not None
        assert r.data.is_conclusive is False
        assert confirmed_session.phase == Phase.ASK_LANGUAGE


class TestPipelineTemplatePath:
    def _settings(self, **extra: object) -> Settings:
        base = dict(
            azdo_org="contoso",
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )
        return Settings(**{**base, **extra})  # type: ignore[arg-type]

    def test_usa_template_da_linguagem(self) -> None:
        s = self._settings(devops_template_go="pipelines/go-k8s-app.yaml")
        assert (
            exec_mod._pipeline_template_path(s, AppType.API, Language.GO)
            == "pipelines/go-k8s-app.yaml"
        )

    def test_linguagem_sem_template_cai_no_default(self) -> None:
        s = self._settings(devops_template_default="pipelines/generico.yaml")
        assert (
            exec_mod._pipeline_template_path(s, AppType.WORKER, Language.PYTHON)
            == "pipelines/generico.yaml"
        )

    def test_web_reaproveita_template_de_node(self) -> None:
        s = self._settings(devops_template_node="pipelines/node-k8s-app.yaml")
        # web ignora a linguagem detectada (passada como dotnet) e usa o de node
        assert (
            exec_mod._pipeline_template_path(s, AppType.WEB, Language.DOTNET)
            == "pipelines/node-k8s-app.yaml"
        )


class TestSingleRepoExecute:
    @respx.mock
    def test_clone_e_pr_unicos_no_repo_da_app(
        self, monkeypatch: pytest.MonkeyPatch, confirmed_session: SessionState
    ) -> None:
        # O Helm vive no proprio repo da app: nao ha segundo clone nem segundo PR.
        settings = Settings(  # type: ignore[call-arg]
            azdo_org="contoso",
            azdo_auth_mode="pat",
            azdo_pat="dummy",
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )
        clones: dict[str, dict[str, object]] = {}
        written: list[list[str]] = []

        def _clone(ws: object, **kw: object) -> ToolResult[Path]:
            clones[str(kw["key"])] = kw
            return ToolResult.success(Path(ws.root) / str(kw["key"]))  # type: ignore[attr-defined]

        def _write(p: Path, files: list[object]) -> ToolResult[int]:
            written.append([f.path for f in files])  # type: ignore[attr-defined]
            return ToolResult.success(len(files))

        monkeypatch.setattr(exec_mod, "clone_shallow", _clone)
        monkeypatch.setattr(exec_mod, "list_existing_paths", lambda p: set())
        monkeypatch.setattr(exec_mod, "write_files", _write)
        monkeypatch.setattr(
            exec_mod, "create_branch_and_commit", lambda p, **kw: ToolResult.success("a" * 40)
        )
        monkeypatch.setattr(exec_mod, "push_branch", lambda p, **kw: ToolResult.success(kw["branch"]))

        bodies: list[dict[str, object]] = []

        def _pr(request: httpx.Request) -> httpx.Response:
            import json as _json

            bodies.append(_json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "pullRequestId": 1,
                    "url": "x",
                    "status": "active",
                    "repository": {"project": {"name": "Platform"}},
                },
            )

        respx.post(url__regex=r".*/pullrequests").mock(side_effect=_pr)

        r = exec_mod.execute(confirmed_session, settings)
        assert r.ok, r.error
        # um unico clone: o do repo da aplicacao
        assert set(clones) == {"app"}
        assert str(clones["app"]["clone_url"]).endswith("/payments-api")
        # o write inclui o Helm junto dos demais arquivos, no mesmo repo
        assert len(written) == 1
        assert "helm/manifests/Chart.yaml" in written[0]
        assert "Dockerfile" in written[0]
        # um unico PR, contra a branch da app
        assert len(bodies) == 1
        assert bodies[0]["targetRefName"] == "refs/heads/main"


class TestPatModeInalterado:
    @respx.mock
    def test_pat_nao_faz_obo_e_abre_pr(
        self, monkeypatch: pytest.MonkeyPatch, confirmed_session: SessionState
    ) -> None:
        pat_settings = Settings(  # type: ignore[call-arg]
            azdo_org="contoso",
            azdo_auth_mode="pat",
            azdo_pat="dummy-pat",
            devops_templates_repo_url="https://dev.azure.com/contoso/Platform/_git/devops-templates",
        )
        _mock_git_ok(monkeypatch)

        perm_route = respx.post(_PERM_URL).mock(return_value=httpx.Response(200))
        respx.post(url__regex=r".*/pullrequests").mock(
            return_value=httpx.Response(
                201,
                json={
                    "pullRequestId": 1,
                    "url": "x",
                    "status": "active",
                    "repository": {"project": {"name": "Platform"}},
                },
            )
        )

        r = exec_mod.execute(confirmed_session, pat_settings)
        assert r.ok, r.error
        # modo pat nao faz check OBO
        assert not perm_route.called
