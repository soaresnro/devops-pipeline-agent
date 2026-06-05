"""Testes das tools de validacao - garantem que o agente recusa inputs incorretos."""

from __future__ import annotations

import pytest

from devops_agent.models import AppType
from devops_agent.tools.validation import (
    validate_app_type,
    validate_branch_name,
    validate_k8s_namespace,
    validate_repo_url,
)


class TestValidateRepoUrl:
    def test_aceita_url_padrao(self) -> None:
        r = validate_repo_url(
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            allowed_org="contoso",
        )
        assert r.ok
        assert r.data is not None
        assert r.data.organization == "contoso"
        assert r.data.project == "Platform"
        assert r.data.repository == "payments-api"

    def test_aceita_url_com_user_no_host(self) -> None:
        r = validate_repo_url(
            "https://contoso@dev.azure.com/contoso/Platform/_git/payments-api",
            allowed_org="contoso",
        )
        assert r.ok

    def test_normaliza_case_insensitivo_da_org(self) -> None:
        r = validate_repo_url(
            "https://dev.azure.com/Contoso/Platform/_git/payments-api",
            allowed_org="contoso",
        )
        assert r.ok

    def test_rejeita_http(self) -> None:
        r = validate_repo_url(
            "http://dev.azure.com/contoso/Platform/_git/payments-api",
            allowed_org="contoso",
        )
        assert not r.ok
        assert r.error_code == "repo.not_https"

    def test_rejeita_org_fora_da_allowlist(self) -> None:
        r = validate_repo_url(
            "https://dev.azure.com/outra/Platform/_git/payments-api",
            allowed_org="contoso",
        )
        assert not r.ok
        assert r.error_code == "repo.org_not_allowed"

    def test_rejeita_url_invalida(self) -> None:
        r = validate_repo_url("https://github.com/x/y", allowed_org="contoso")
        assert not r.ok
        assert r.error_code == "repo.bad_format"

    def test_rejeita_vazio(self) -> None:
        r = validate_repo_url("", allowed_org="contoso")
        assert not r.ok
        assert r.error_code == "repo.empty"


class TestValidateK8sNamespace:
    @pytest.mark.parametrize(
        "ns",
        ["payments-prod", "app", "a1", "abc-123", "myapp"],
    )
    def test_aceita_namespaces_validos(self, ns: str) -> None:
        r = validate_k8s_namespace(ns)
        assert r.ok, r.error

    def test_normaliza_para_lowercase(self) -> None:
        r = validate_k8s_namespace("PaymentsProd")
        # tolerancia de input: maiuscula vira minuscula
        assert r.ok
        assert r.data == "paymentsprod"

    @pytest.mark.parametrize(
        ("ns", "expected_code"),
        [
            ("", "ns.empty"),
            ("-leading", "ns.invalid_format"),
            ("trailing-", "ns.invalid_format"),
            ("under_score", "ns.invalid_format"),
            ("x" * 64, "ns.too_long"),
            ("default", "ns.reserved"),
            ("kube-system", "ns.reserved"),
            ("kube-anything", "ns.reserved_prefix"),
            ("azure-stuff", "ns.reserved_prefix"),
            ("istio-mesh", "ns.reserved_prefix"),
        ],
    )
    def test_rejeita_invalidos(self, ns: str, expected_code: str) -> None:
        r = validate_k8s_namespace(ns)
        assert not r.ok
        assert r.error_code == expected_code


class TestValidateAppType:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("api", AppType.API),
            ("API", AppType.API),
            (" worker ", AppType.WORKER),
            ("web", AppType.WEB),
        ],
    )
    def test_aceita_tipos_validos(self, entrada: str, esperado: AppType) -> None:
        r = validate_app_type(entrada)
        assert r.ok
        assert r.data == esperado

    @pytest.mark.parametrize("entrada", ["", "function", "lambda", "service"])
    def test_rejeita_tipos_invalidos(self, entrada: str) -> None:
        r = validate_app_type(entrada)
        assert not r.ok


class TestValidateBranchName:
    @pytest.mark.parametrize(
        "branch",
        ["main", "master", "develop", "feature/abc", "release/v1.2", "hotfix-2026-05"],
    )
    def test_aceita_branches_validas(self, branch: str) -> None:
        r = validate_branch_name(branch)
        assert r.ok, r.error

    def test_remove_prefixo_refs_heads(self) -> None:
        r = validate_branch_name("refs/heads/main")
        assert r.ok
        assert r.data == "main"

    @pytest.mark.parametrize(
        "branch",
        ["/leading", "trailing/", "with space", "double..dot", "@{weird}"],
    )
    def test_rejeita_branches_invalidas(self, branch: str) -> None:
        r = validate_branch_name(branch)
        assert not r.ok
