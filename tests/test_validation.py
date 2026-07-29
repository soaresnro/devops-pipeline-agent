"""Testes das tools de validacao - garantem que o agente recusa inputs incorretos."""

from __future__ import annotations

import pytest

from devops_agent.models import AppType, Language
from devops_agent.tools.validation import (
    APIM_ENVIRONMENTS,
    validate_api_ids,
    validate_app_type,
    validate_branch_name,
    validate_cron_schedule,
    validate_dotnet_entrypoint,
    validate_dotnet_version,
    validate_k8s_namespace,
    validate_language,
    validate_python_entrypoint,
    validate_python_package_manager,
    validate_python_version,
    validate_repo_url,
)


class TestValidateDotnetEntrypoint:
    def test_adiciona_dll_se_faltar(self) -> None:
        r = validate_dotnet_entrypoint("ePharma.Authentication.Services")
        assert r.ok
        assert r.data == "ePharma.Authentication.Services.dll"

    def test_mantem_dll_existente(self) -> None:
        r = validate_dotnet_entrypoint("App.dll")
        assert r.ok
        assert r.data == "App.dll"

    @pytest.mark.parametrize("entrada", ["", "  ", "nome com espaco", "x/y"])
    def test_rejeita_invalidos(self, entrada: str) -> None:
        r = validate_dotnet_entrypoint(entrada)
        assert not r.ok


class TestValidateDotnetVersion:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("", "8.0.x"),
            ("   ", "8.0.x"),
            ("8.0.x", "8.0.x"),
            ("6.0.x", "6.0.x"),
            ("9.0.x", "9.0.x"),
            ("v8.0.x", "8.0.x"),
            ("8.0.100", "8.0.100"),
        ],
    )
    def test_aceita_versoes_validas(self, entrada: str, esperado: str) -> None:
        r = validate_dotnet_version(entrada)
        assert r.ok, r.error
        assert r.data == esperado

    @pytest.mark.parametrize("entrada", ["10.0.x", "8.1.x", "abc", "8"])
    def test_rejeita_versoes_invalidas(self, entrada: str) -> None:
        r = validate_dotnet_version(entrada)
        assert not r.ok
        assert r.error_code == "dotnet_version.invalid"


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

    def test_aceita_url_legada_visualstudio_com(self) -> None:
        # org no subdominio; projeto pode vir url-encoded
        r = validate_repo_url(
            "https://contoso.visualstudio.com/Squad%20Engenharia/_git/epharma-api-auth",
            allowed_org="contoso",
        )
        assert r.ok, r.error
        assert r.data is not None
        assert r.data.organization == "contoso"
        assert r.data.project == "Squad%20Engenharia"
        assert r.data.repository == "epharma-api-auth"

    def test_visualstudio_com_respeita_allowlist(self) -> None:
        r = validate_repo_url(
            "https://outraorg.visualstudio.com/Proj/_git/repo",
            allowed_org="contoso",
        )
        assert not r.ok
        assert r.error_code == "repo.org_not_allowed"

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
            ("cronjob", AppType.CRONJOB),
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


class TestValidateLanguage:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("dotnet", Language.DOTNET),
            (".NET", Language.DOTNET),
            ("C#", Language.DOTNET),
            ("node", Language.NODE),
            ("nodejs", Language.NODE),
            (" JS ", Language.NODE),
            ("typescript", Language.NODE),
            ("python", Language.PYTHON),
            ("py", Language.PYTHON),
            ("go", Language.GO),
            ("golang", Language.GO),
        ],
    )
    def test_aceita_linguagens_e_aliases(self, entrada: str, esperado: Language) -> None:
        r = validate_language(entrada)
        assert r.ok, r.error
        assert r.data == esperado

    @pytest.mark.parametrize("entrada", ["", "rust", "cobol", "fortran"])
    def test_rejeita_linguagens_invalidas(self, entrada: str) -> None:
        r = validate_language(entrada)
        assert not r.ok
        assert r.error_code in {"language.empty", "language.invalid"}


class TestValidateCronSchedule:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("0 2 * * *", "0 2 * * *"),
            ("*/15 * * * *", "*/15 * * * *"),
            ("0 0 1,15 * *", "0 0 1,15 * *"),
            ("  0   2   *   *   *  ", "0 2 * * *"),  # normaliza espacos
            ("@daily", "@daily"),
            ("@hourly", "@hourly"),
        ],
    )
    def test_aceita_cron_valido(self, entrada: str, esperado: str) -> None:
        r = validate_cron_schedule(entrada)
        assert r.ok, r.error
        assert r.data == esperado

    @pytest.mark.parametrize(
        ("entrada", "code"),
        [
            ("", "cron.empty"),
            ("0 2 * *", "cron.bad_field_count"),  # 4 campos
            ("0 2 * * * *", "cron.bad_field_count"),  # 6 campos
            ("0 2 * * seg", "cron.invalid_format"),  # caractere invalido
            ("@yearlyish", "cron.bad_field_count"),  # macro desconhecido vira 1 campo
        ],
    )
    def test_rejeita_cron_invalido(self, entrada: str, code: str) -> None:
        r = validate_cron_schedule(entrada)
        assert not r.ok
        assert r.error_code == code


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


class TestValidatePythonVersion:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("3.12", "3.12"),
            ("3.9", "3.9"),
            ("3.13", "3.13"),
            ("3.12.4", "3.12.4"),
            ("  3.11  ", "3.11"),
            ("python3.12", "3.12"),
            ("v3.12", "3.12"),
        ],
    )
    def test_versoes_validas(self, entrada: str, esperado: str) -> None:
        r = validate_python_version(entrada)
        assert r.ok, r.error
        assert r.data == esperado

    @pytest.mark.parametrize("entrada", ["", "   "])
    def test_vazio_usa_default(self, entrada: str) -> None:
        r = validate_python_version(entrada)
        assert r.ok
        assert r.data == "3.12"

    @pytest.mark.parametrize("entrada", ["2.7", "4.0", "3", "3.x", "latest", "3.12.4.1"])
    def test_rejeita_invalidas(self, entrada: str) -> None:
        r = validate_python_version(entrada)
        assert not r.ok
        assert r.error_code == "python_version.invalid"


class TestValidatePythonPackageManager:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("pip", "pip"),
            ("PIP", "pip"),
            ("requirements.txt", "pip"),
            ("poetry", "poetry"),
            ("pyproject.toml", "poetry"),
        ],
    )
    def test_aceita_aliases(self, entrada: str, esperado: str) -> None:
        r = validate_python_package_manager(entrada)
        assert r.ok, r.error
        assert r.data == esperado

    def test_vazio_usa_pip(self) -> None:
        r = validate_python_package_manager("  ")
        assert r.ok
        assert r.data == "pip"

    @pytest.mark.parametrize("entrada", ["pipenv", "conda", "uv"])
    def test_rejeita_fora_do_contrato_do_template(self, entrada: str) -> None:
        # o template so declara pip|poetry; outro valor quebra a compilacao
        r = validate_python_package_manager(entrada)
        assert not r.ok
        assert r.error_code == "python_package_manager.invalid"


class TestValidatePythonEntrypoint:
    @pytest.mark.parametrize(
        "entrada",
        [
            "uvicorn main:app --host 0.0.0.0 --port 8080",
            "python -m app.worker",
            "gunicorn app.main:app --bind 0.0.0.0:8080 --workers 4",
            "celery -A app.tasks worker --loglevel=info",
        ],
    )
    def test_comandos_validos(self, entrada: str) -> None:
        r = validate_python_entrypoint(entrada)
        assert r.ok, r.error
        assert r.data == entrada

    def test_normaliza_espacos(self) -> None:
        r = validate_python_entrypoint("  python   -m   app  ")
        assert r.ok
        assert r.data == "python -m app"

    @pytest.mark.parametrize("entrada", ["", "   "])
    def test_rejeita_vazio(self, entrada: str) -> None:
        r = validate_python_entrypoint(entrada)
        assert not r.ok
        assert r.error_code == "python_entrypoint.empty"

    @pytest.mark.parametrize(
        "entrada",
        [
            "python app.py && echo ok",
            "sh -c 'python app.py'",
            "python app.py > /tmp/log",
            "python app.py $PORT",
        ],
    )
    def test_rejeita_sintaxe_de_shell(self, entrada: str) -> None:
        # o CMD e gerado em exec form: nao ha shell para interpretar esses operadores
        r = validate_python_entrypoint(entrada)
        assert not r.ok
        assert r.error_code == "python_entrypoint.shell_syntax"

    def test_rejeita_caractere_invalido(self) -> None:
        r = validate_python_entrypoint("python app.py --nome joão")
        assert not r.ok
        assert r.error_code == "python_entrypoint.invalid"


class TestValidateApiIds:
    """Aceita duas formas: um valor unico (vale para os quatro ambientes) ou os quatro
    separados por virgula na ordem dev,qa,uat,prod."""

    def test_single_replica_o_valor_unico(self) -> None:
        # stack python: o template declara um unico apiId
        r = validate_api_ids("api-utils", single=True)
        assert r.ok, r.error
        assert r.data == dict.fromkeys(APIM_ENVIRONMENTS, "api-utils")

    @pytest.mark.parametrize("valor", ["a,b", "a-dev,a-qa,a-uat,a-prod"])
    def test_single_rejeita_lista(self, valor: str) -> None:
        r = validate_api_ids(valor, single=True)
        assert not r.ok
        assert r.error_code == "api_ids.single_only"

    def test_ordem_das_chaves_e_o_contrato_da_pipeline(self) -> None:
        assert APIM_ENVIRONMENTS == ("dev", "qa", "uat", "prod")

    def test_quatro_valores_mapeiam_na_ordem(self) -> None:
        r = validate_api_ids("a-dev,a-qa,a-uat,a-prod")
        assert r.ok, r.error
        assert r.data == {"dev": "a-dev", "qa": "a-qa", "uat": "a-uat", "prod": "a-prod"}

    def test_valor_unico_se_replica_nos_quatro(self) -> None:
        r = validate_api_ids("api-utils")
        assert r.ok, r.error
        assert r.data == dict.fromkeys(APIM_ENVIRONMENTS, "api-utils")

    def test_ignora_espacos_em_volta_das_virgulas(self) -> None:
        r = validate_api_ids("  a-dev , a-qa ,a-uat,  a-prod  ")
        assert r.ok, r.error
        assert r.data is not None
        assert r.data["qa"] == "a-qa"

    @pytest.mark.parametrize("valor", ["", "   ", "\n"])
    def test_rejeita_vazio(self, valor: str) -> None:
        r = validate_api_ids(valor)
        assert not r.ok
        assert r.error_code == "api_ids.empty"

    @pytest.mark.parametrize("valor", ["a,b", "a,b,c", "a,b,c,d,e"])
    def test_rejeita_quantidade_diferente_de_1_ou_4(self, valor: str) -> None:
        r = validate_api_ids(valor)
        assert not r.ok
        assert r.error_code == "api_ids.count"

    def test_rejeita_posicao_vazia_e_aponta_o_ambiente(self) -> None:
        r = validate_api_ids("a-dev,,a-uat,a-prod")
        assert not r.ok
        assert r.error_code == "api_ids.empty_item"
        assert "qa" in (r.error or "")

    @pytest.mark.parametrize(
        ("valor", "ambiente"),
        [
            ("-abre-com-hifen,a,b,c", "dev"),
            ("a,com espaco,b,c", "qa"),
            ("a,b,integraçao,c", "uat"),
            ("a,b,c,barra/invalida", "prod"),
        ],
    )
    def test_rejeita_caractere_invalido_e_aponta_o_ambiente(
        self, valor: str, ambiente: str
    ) -> None:
        r = validate_api_ids(valor)
        assert not r.ok
        assert r.error_code == "api_ids.invalid"
        assert ambiente in (r.error or "")

    def test_rejeita_valor_longo_demais(self) -> None:
        r = validate_api_ids("a" * 121)
        assert not r.ok
        assert r.error_code == "api_ids.invalid"
