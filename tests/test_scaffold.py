"""Testes da tool de scaffold - garantem que templates renderizam consistentemente."""

from __future__ import annotations

import pytest
import yaml

from devops_agent.models import AppType, Language
from devops_agent.tools.scaffold import _pipeline_stack, build_bundle, dry_run


class TestPipelineStack:
    def test_dotnet_nao_web_usa_stack_dotnet(self) -> None:
        assert _pipeline_stack(AppType.API, Language.DOTNET) == "dotnet"
        assert _pipeline_stack(AppType.CRONJOB, Language.DOTNET) == "dotnet"

    def test_python_nao_web_usa_stack_python(self) -> None:
        assert _pipeline_stack(AppType.API, Language.PYTHON) == "python"
        assert _pipeline_stack(AppType.WORKER, Language.PYTHON) == "python"
        assert _pipeline_stack(AppType.CRONJOB, Language.PYTHON) == "python"

    def test_sem_template_real_usa_default(self) -> None:
        assert _pipeline_stack(AppType.API, Language.GO) == "_default"
        assert _pipeline_stack(AppType.WORKER, Language.NODE) == "_default"

    def test_web_sempre_default(self) -> None:
        # web ignora a linguagem; nao usa os stacks dotnet/python
        assert _pipeline_stack(AppType.WEB, Language.DOTNET) == "_default"
        assert _pipeline_stack(AppType.WEB, Language.PYTHON) == "_default"


@pytest.fixture
def base_ctx() -> dict[str, object]:
    return {
        "app_name": "payments-api",
        "namespace": "payments-prod",
        "branch": "main",
        "acr_name": "contosoacr",
        "devops_templates_repo_name": "Platform/devops-templates",
        "devops_templates_tag": "v3",
    }


class TestBuildBundle:
    @pytest.mark.parametrize("app_type", list(AppType))
    def test_sempre_contem_dockerfile_pipeline_e_helm_no_repo_da_app(
        self,
        app_type: AppType,
        base_ctx: dict[str, object],
    ) -> None:
        # bundle unico: tudo (incluindo o Helm) vai para o repo da propria app
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = bundle.all_paths
        assert "Dockerfile" in paths
        assert "azure-pipelines.yaml" in paths
        assert "helm/base/Chart.yaml" in paths
        assert "helm/base/values.yaml" in paths

    @pytest.mark.parametrize("app_type", [AppType.API, AppType.WEB, AppType.WORKER])
    def test_base_chart_apps_sao_charts_finas_com_dependency(
        self,
        app_type: AppType,
        base_ctx: dict[str, object],
    ) -> None:
        # api/web/worker: chart fina (Chart.yaml + values.yaml), SEM templates proprios
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_paths)
        chart_root = "helm/base"
        assert f"{chart_root}/Chart.yaml" in paths
        assert f"{chart_root}/values.yaml" in paths
        assert not any(p.startswith(f"{chart_root}/templates/") for p in paths)
        # Chart.yaml declara dependency na base chart com repository apontando para o
        # repo do time (AksHelmBaseCharts); o helm resolve pelo .tgz em charts/.
        chart = next(f for f in bundle.files if f.path == f"{chart_root}/Chart.yaml")
        dep = yaml.safe_load(chart.content)["dependencies"][0]
        assert dep["name"] in ("microservice-chart", "worker-chart")
        assert dep["repository"].startswith("file://")
        assert "AksHelmBaseCharts" in dep["repository"]

    @pytest.mark.parametrize(
        ("app_type", "base_chart"),
        [
            (AppType.API, "microservice-chart"),
            (AppType.WEB, "microservice-chart"),
            (AppType.WORKER, "worker-chart"),
        ],
    )
    def test_base_chart_por_tipo(
        self, app_type: AppType, base_chart: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        chart = next(f for f in bundle.files if f.path == "helm/base/Chart.yaml")
        assert yaml.safe_load(chart.content)["dependencies"][0]["name"] == base_chart

    def test_cronjob_e_self_contained(self, base_ctx: dict[str, object]) -> None:
        # cronjob nao tem base chart -> mantem o chart self-contained com templates
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_paths)
        chart_root = "helm/base"
        assert f"{chart_root}/templates/cronjob.yaml" in paths
        assert f"{chart_root}/templates/_helpers.tpl" in paths
        assert f"{chart_root}/templates/serviceaccount.yaml" in paths


class TestVendoredBaseChart:
    """A chart fina vendoriza a base chart EMPACOTADA em charts/<base>-<ver>.tgz + o
    Chart.lock correspondente (par consistente do repo modelo), para que o helm resolva a
    dependency declarada no Chart.yaml a partir do .tgz."""

    @pytest.mark.parametrize(
        ("app_type", "base_chart", "version"),
        [
            (AppType.API, "microservice-chart", "0.4.5"),
            (AppType.WEB, "microservice-chart", "0.4.5"),
            (AppType.WORKER, "worker-chart", "1.0.0"),
        ],
    )
    def test_emite_tgz_e_chart_lock(
        self, app_type: AppType, base_chart: str, version: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_paths)
        assert f"helm/base/charts/{base_chart}-{version}.tgz" in paths
        assert "helm/base/Chart.lock" in paths

    def test_tgz_e_binario_e_nao_vazio(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        tgz = next(f for f in bundle.files if f.path.endswith(".tgz"))
        assert tgz.is_binary
        assert tgz.binary_b64
        assert tgz.content == ""

    def test_versao_do_tgz_e_lock_casam_com_a_dependency(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        chart = next(f for f in bundle.files if f.path == "helm/base/Chart.yaml")
        lock = next(f for f in bundle.files if f.path == "helm/base/Chart.lock")
        chart_dep = yaml.safe_load(chart.content)["dependencies"][0]
        lock_dep = yaml.safe_load(lock.content)["dependencies"][0]
        assert chart_dep["name"] == lock_dep["name"] == "worker-chart"
        assert chart_dep["version"] == lock_dep["version"] == "1.0.0"
        # o .tgz emitido usa a mesma versao
        assert "helm/base/charts/worker-chart-1.0.0.tgz" in bundle.all_paths

    def test_cronjob_nao_vendoriza_base_chart(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        assert not any(p.startswith("helm/base/charts/") for p in bundle.all_paths)
        assert not any(f.path.endswith(".tgz") for f in bundle.files)
        assert "helm/base/Chart.lock" not in bundle.all_paths


class TestEnvironments:
    def test_gera_override_por_ambiente_default(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_paths)
        for env in ("dev", "qa", "uat", "prod"):
            assert f"helm/environments/{env}/values.yaml" in paths

    def test_environments_customizado(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, environments=["dev", "prod"], **base_ctx  # type: ignore[arg-type]
        )
        paths = set(bundle.all_paths)
        assert "helm/environments/dev/values.yaml" in paths
        assert "helm/environments/prod/values.yaml" in paths
        assert "helm/environments/qa/values.yaml" not in paths

    def test_dev_values_e_gerado_preenchido(self, base_ctx: dict[str, object]) -> None:
        # a referencia deixa dev vazio, mas o agente gera o override completo tambem para dev
        bundle = build_bundle(
            app_type=AppType.API, ingress_host="servicesutils", **base_ctx  # type: ignore[arg-type]
        )
        dev = next(
            f for f in bundle.files if f.path == "helm/environments/dev/values.yaml"
        )
        assert dev.content != ""
        mc = yaml.safe_load(dev.content)["microservice-chart"]
        assert mc["environment"] == "dev"
        assert mc["image"]["repository"] == "acraksdeveastus.azurecr.io/payments-api"
        assert mc["ingresses"][0]["rules"][0]["host"] == "servicesutils.k8s.dev.epharma.com.br"

    def test_override_microservice_skeleton_dois_ingresses(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            application_name="api-utils",
            apim_path="utils",
            ingress_host="servicesutils",
            **base_ctx,  # type: ignore[arg-type]
        )
        ev = next(
            f for f in bundle.files if f.path == "helm/environments/qa/values.yaml"
        )
        mc = yaml.safe_load(ev.content)["microservice-chart"]
        # registry por ambiente + tag placeholder substituida pela pipeline
        assert mc["image"]["repository"] == "acraksqaeastus.azurecr.io/payments-api"
        assert mc["image"]["tag"] == "__imageTag__"
        assert mc["environment"] == "qa"
        # dois ingresses (primary private-nginx + legacy private-legacy-nginx) com hosts derivados
        assert len(mc["ingresses"]) == 2
        primary, legacy = mc["ingresses"]
        assert primary["name"] == "api-utils"
        assert primary["className"] == "private-nginx"
        assert primary["rules"][0]["host"] == "servicesutils.k8s.qa.epharma.com.br"
        assert legacy["name"] == "api-utils-legacy"
        assert legacy["className"] == "private-legacy-nginx"
        assert legacy["rules"][0]["host"] == "servicesutilsqa.epharma.com.br"
        # SA no formato sa-{namespace}-{applicationName}
        assert mc["serviceAccount"]["name"] == "sa-payments-prod-api-utils"
        # env defaults: timezone (todos) + ASPNETCORE_ENVIRONMENT (por ambiente) + Swagger (api)
        assert mc["env"]["WEBSITE_TIME_ZONE"] == "America/Sao_Paulo"
        assert mc["env"]["ASPNETCORE_ENVIRONMENT"] == "Development"  # qa -> Development
        assert mc["env"]["SwaggerEnabled"] == "true"

    def test_env_aspnetcore_environment_por_ambiente(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        esperado = {
            "dev": "Development",
            "qa": "Development",
            "uat": "Staging",
            "prod": "Production",
        }
        for env, aspnet in esperado.items():
            ev = next(
                f for f in bundle.files if f.path == f"helm/environments/{env}/values.yaml"
            )
            mc = yaml.safe_load(ev.content)["microservice-chart"]
            assert mc["env"]["ASPNETCORE_ENVIRONMENT"] == aspnet
            assert mc["env"]["WEBSITE_TIME_ZONE"] == "America/Sao_Paulo"

    def test_env_swagger_so_api(self, base_ctx: dict[str, object]) -> None:
        # SwaggerEnabled so em api; web (mesmo microservice-chart) nao recebe
        api = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        web = build_bundle(app_type=AppType.WEB, **base_ctx)  # type: ignore[arg-type]
        api_env = yaml.safe_load(
            next(f for f in api.files if f.path == "helm/environments/prod/values.yaml").content
        )["microservice-chart"]["env"]
        web_env = yaml.safe_load(
            next(f for f in web.files if f.path == "helm/environments/prod/values.yaml").content
        )["microservice-chart"]["env"]
        assert api_env["SwaggerEnabled"] == "true"
        assert "SwaggerEnabled" not in web_env
        assert web_env["ASPNETCORE_ENVIRONMENT"] == "Production"

    def test_prod_omite_segmento_de_ambiente_no_host(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.API, ingress_host="servicesutils", **base_ctx  # type: ignore[arg-type]
        )
        ev = next(
            f for f in bundle.files if f.path == "helm/environments/prod/values.yaml"
        )
        mc = yaml.safe_load(ev.content)["microservice-chart"]
        assert mc["ingresses"][0]["rules"][0]["host"] == "servicesutils.k8s.epharma.com.br"
        assert mc["ingresses"][1]["rules"][0]["host"] == "servicesutils.epharma.com.br"

    def test_application_name_remove_prefixo_do_namespace(self) -> None:
        # ns=autorizador + applicationName=autorizador-api-teste: o prefixo do namespace
        # e removido, entao o SA nao duplica o namespace (sa-autorizador-api-teste).
        bundle = build_bundle(
            app_name="autorizador-api-teste",
            app_type=AppType.API,
            namespace="autorizador",
            branch="main",
            acr_name="contosoacr",
            devops_templates_repo_name="Platform/devops-templates",
            devops_templates_tag="v3",
            application_name="autorizador-api-teste",
            api_ids=dict.fromkeys(("dev", "qa", "uat", "prod"), "api-teste"),
        )
        ev = next(
            f for f in bundle.files if f.path == "helm/environments/qa/values.yaml"
        )
        mc = yaml.safe_load(ev.content)["microservice-chart"]
        assert mc["serviceAccount"]["name"] == "sa-autorizador-api-teste"
        assert mc["ingresses"][0]["name"] == "api-teste"
        params = yaml.safe_load(
            next(f for f in bundle.files if f.path == "azure-pipelines.yaml").content
        )["extends"]["parameters"]
        assert params["applicationName"] == "api-teste"

    def test_override_worker_sem_ingresses(self, base_ctx: dict[str, object]) -> None:
        # worker usa o worker-chart e um env nao-dev (dev e vazio para todos os tipos)
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        ev = next(
            f for f in bundle.files if f.path == "helm/environments/prod/values.yaml"
        )
        wc = yaml.safe_load(ev.content)["worker-chart"]
        assert wc["image"]["repository"] == "acraksprodeastus.azurecr.io/payments-api"
        assert "ingresses" not in wc


class TestEnvDoDockerfileNoValues:
    """A unica ENV do Dockerfile e DOTNET_RUNNING_IN_CONTAINER (descreve a imagem); as
    demais variaveis de runtime vivem no values de cada ambiente, ajustaveis sem rebuild."""

    _MOVIDAS = (
        "TZ",
        "ASPNETCORE_URLS",
        "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT",
        "Logging__LogLevel__Default",
        "Logging__LogLevel__Microsoft",
        "Logging__LogLevel__Microsoft.Hosting.Lifetime",
    )

    @pytest.mark.parametrize("app_type", [AppType.API, AppType.WORKER, AppType.CRONJOB])
    def test_dockerfile_dotnet_tem_uma_unica_env(
        self, app_type: AppType, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        envs = [ln.strip() for ln in df.splitlines() if ln.startswith("ENV ")]
        assert envs == ["ENV DOTNET_RUNNING_IN_CONTAINER=true"]

    def test_values_api_recebem_as_envs_movidas(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        for env in ("dev", "prod"):
            ev = next(f for f in bundle.files if f.path == f"helm/environments/{env}/values.yaml")
            app_env = yaml.safe_load(ev.content)["microservice-chart"]["env"]
            for key in self._MOVIDAS:
                assert key in app_env, f"{key} ausente em {env}"
            assert app_env["TZ"] == "America/Sao_Paulo"
            assert app_env["ASPNETCORE_URLS"] == "http://+:8080"
            assert app_env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] == "false"
            assert app_env["Logging__LogLevel__Microsoft.Hosting.Lifetime"] == "Information"
            # as ENVs movidas convivem com os defaults por ambiente que ja existiam
            assert app_env["ASPNETCORE_ENVIRONMENT"]

    def test_values_worker_recebem_as_envs_movidas_sem_aspnetcore_urls(
        self, base_ctx: dict[str, object]
    ) -> None:
        # worker roda em dotnet/runtime (nao aspnet): nao escuta HTTP, entao sem ASPNETCORE_URLS
        bundle = build_bundle(app_type=AppType.WORKER, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == "helm/environments/qa/values.yaml")
        app_env = yaml.safe_load(ev.content)["worker-chart"]["env"]
        assert app_env["TZ"] == "America/Sao_Paulo"
        assert app_env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] == "false"
        assert "ASPNETCORE_URLS" not in app_env

    def test_values_cronjob_recebem_as_envs_movidas_como_lista(
        self, base_ctx: dict[str, object]
    ) -> None:
        # o chart self-contained do cronjob usa env como lista de {name, value}
        bundle = build_bundle(app_type=AppType.CRONJOB, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == "helm/environments/qa/values.yaml")
        app_env = {item["name"]: item["value"] for item in yaml.safe_load(ev.content)["env"]}
        assert app_env["TZ"] == "America/Sao_Paulo"
        assert app_env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] == "false"
        assert "ASPNETCORE_URLS" not in app_env

    def test_web_nao_recebe_envs_dotnet(self, base_ctx: dict[str, object]) -> None:
        # web e SPA estatica servida por nginx, independente da linguagem detectada
        bundle = build_bundle(app_type=AppType.WEB, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == "helm/environments/prod/values.yaml")
        app_env = yaml.safe_load(ev.content)["microservice-chart"]["env"]
        for key in self._MOVIDAS:
            assert key not in app_env

    @pytest.mark.parametrize("language", [Language.NODE, Language.PYTHON, Language.GO])
    def test_outras_linguagens_nao_recebem_envs_dotnet(
        self, language: Language, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.API, language=language, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == "helm/environments/prod/values.yaml")
        app_env = yaml.safe_load(ev.content)["microservice-chart"]["env"]
        # TZ fica de fora da lista: e comum a mais de um stack (python tambem emite)
        for key in (k for k in self._MOVIDAS if k != "TZ"):
            assert key not in app_env
        # defaults do runtime .NET tambem nao valem para outras linguagens
        assert "ASPNETCORE_ENVIRONMENT" not in app_env
        assert "SwaggerEnabled" not in app_env


class TestDatadogUnifiedServiceTagging:
    """podLabels + DD_* apenas em uat/prod (ambientes com agente do Datadog coletando).

    O nome do servico segue {namespace}-{applicationName}-k8s-{env}; o applicationName
    ja vem sem o prefixo do namespace (strip_namespace_prefix), entao nao duplica.
    """

    _TAGS = (
        "tags.datadoghq.com/env",
        "tags.datadoghq.com/service",
        "tags.datadoghq.com/version",
    )

    @pytest.mark.parametrize(
        ("app_type", "chave"),
        [
            (AppType.API, "microservice-chart"),
            (AppType.WEB, "microservice-chart"),
            (AppType.WORKER, "worker-chart"),
        ],
    )
    @pytest.mark.parametrize("env", ["uat", "prod"])
    def test_uat_e_prod_recebem_podlabels_e_dd_envs(
        self, app_type: AppType, chave: str, env: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == f"helm/environments/{env}/values.yaml")
        secao = yaml.safe_load(ev.content)[chave]
        servico = f"payments-prod-payments-api-k8s-{env}"
        assert secao["podLabels"] == {
            "tags.datadoghq.com/env": env,
            "tags.datadoghq.com/service": servico,
            "tags.datadoghq.com/version": "1.0",
        }
        assert secao["env"]["DD_ENV"] == env
        assert secao["env"]["DD_SERVICE"] == servico
        assert secao["env"]["DD_VERSION"] == "1.0"

    @pytest.mark.parametrize(
        ("app_type", "chave"),
        [
            (AppType.API, "microservice-chart"),
            (AppType.WEB, "microservice-chart"),
            (AppType.WORKER, "worker-chart"),
        ],
    )
    @pytest.mark.parametrize("env", ["dev", "qa"])
    def test_dev_e_qa_nao_recebem_datadog(
        self, app_type: AppType, chave: str, env: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        ev = next(f for f in bundle.files if f.path == f"helm/environments/{env}/values.yaml")
        secao = yaml.safe_load(ev.content)[chave]
        assert "podLabels" not in secao
        assert not [k for k in secao.get("env", {}) if k.startswith("DD_")]

    def test_cronjob_uat_recebe_datadog_no_formato_do_chart(
        self, base_ctx: dict[str, object]
    ) -> None:
        # chart self-contained: podLabels no topo e env como lista de {name, value}
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        uat = yaml.safe_load(
            next(f for f in bundle.files if f.path == "helm/environments/uat/values.yaml").content
        )
        assert uat["podLabels"]["tags.datadoghq.com/service"] == "payments-prod-payments-api-k8s-uat"
        app_env = {item["name"]: item["value"] for item in uat["env"]}
        assert app_env["DD_SERVICE"] == "payments-prod-payments-api-k8s-uat"
        assert app_env["DD_VERSION"] == "1.0"
        dev = yaml.safe_load(
            next(f for f in bundle.files if f.path == "helm/environments/dev/values.yaml").content
        )
        assert "podLabels" not in dev

    def test_cronjob_chart_renderiza_podlabels(self, base_ctx: dict[str, object]) -> None:
        # o chart do cronjob e nosso, entao o template le .Values.podLabels
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        cronjob = next(
            f for f in bundle.files if f.path == "helm/base/templates/cronjob.yaml"
        ).content
        assert ".Values.podLabels" in cronjob
        base_values = yaml.safe_load(
            next(f for f in bundle.files if f.path == "helm/base/values.yaml").content
        )
        assert base_values["podLabels"] == {}

    def test_service_name_usa_application_name_sem_prefixo_do_namespace(self) -> None:
        # ns=autorizador + repo autorizador-business-rules-api -> o namespace nao duplica
        bundle = build_bundle(
            app_name="autorizador-business-rules-api",
            app_type=AppType.API,
            namespace="autorizador",
            branch="main",
            acr_name="contosoacr",
            devops_templates_repo_name="Platform/devops-templates",
            devops_templates_tag="v3",
            application_name="autorizador-business-rules-api",
            api_ids=dict.fromkeys(("dev", "qa", "uat", "prod"), "br-api"),
        )
        ev = next(f for f in bundle.files if f.path == "helm/environments/uat/values.yaml")
        mc = yaml.safe_load(ev.content)["microservice-chart"]
        esperado = "autorizador-business-rules-api-k8s-uat"
        assert mc["env"]["DD_SERVICE"] == esperado
        assert mc["podLabels"]["tags.datadoghq.com/service"] == esperado


class TestPipelinePython:
    """Contrato do template-aks-python: parametros declarados la, nada mais.

    Emitir parametro nao declarado quebra a compilacao da pipeline no Azure DevOps.
    """

    # Nomes exatos declarados em pipelines/template-aks-python.yml (AzurePipelinesTemplates).
    _PARAMS_DECLARADOS = frozenset(
        {
            "deploymentStrategy",
            "deployAKS",
            "ApplicationType",
            "projectType",
            "pythonVersion",
            "packageManager",
            "requirementsFile",
            "workingDirectory",
            "sourceFolder",
            "applicationName",
            "namespace",
            "runLint",
            "lintTool",
            "runTests",
            "testPath",
            "testRequirementsFile",
            "minimumCoverage",
            "usePrivateFeed",
            "pythonFeed",
            "imageName",
            "imageTag",
            "dockerfilePath",
            "failOnVulnerabilities",
            "pathApiManagementService",
            "apiId",
            "pathAutomation",
            "keyvault",
            "enableApim",
            "condition",
            "generatePackage",
            "projectsToPackage",
            "vstsFeed",
        }
    )

    def _params(self, **kwargs: object) -> dict[str, object]:
        bundle = build_bundle(language=Language.PYTHON, **kwargs)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        return dict(yaml.safe_load(pipeline.content)["extends"]["parameters"])

    @pytest.mark.parametrize("app_type", [AppType.API, AppType.WORKER, AppType.CRONJOB])
    def test_so_emite_parametros_declarados_no_template(
        self, app_type: AppType, base_ctx: dict[str, object]
    ) -> None:
        params = self._params(app_type=app_type, **base_ctx)
        assert set(params) <= self._PARAMS_DECLARADOS

    def test_estrutura_do_modelo_do_time(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.PYTHON,
            pipeline_template_path="pipelines/template-aks-python.yml",
            **base_ctx,  # type: ignore[arg-type]
        )
        parsed = yaml.safe_load(
            next(f for f in bundle.files if f.path == "azure-pipelines.yaml").content
        )
        assert parsed["name"] == "$(Build.SourceBranchName).$(Build.BuildId)"
        assert parsed["trigger"]["branches"]["include"] == [
            "master",
            "develop",
            "release/*",
            "hotfix/*",
            "feature/*",
        ]
        repos = {r["repository"]: r for r in parsed["resources"]["repositories"]}
        # como o dotnet, referencia o repo de templates por BRANCH
        assert repos["templates"]["ref"] == "refs/heads/master"
        assert "manifests" not in repos
        assert parsed["pool"]["name"] == "agt_pool_linux_vmss"
        var_groups = [v["group"] for v in parsed["variables"] if "group" in v]
        assert "Sonarqube" in var_groups and "k8s-prod" in var_groups
        # alias @templates; o caminho vem do executor (ver _STACK_TEMPLATE_DEFAULTS)
        assert parsed["extends"]["template"] == "pipelines/template-aks-python.yml@templates"

    def test_imageTag_e_variavel_alem_de_parametro(self, base_ctx: dict[str, object]) -> None:
        # a task Tokenizer troca __imageTag__ nos values do Helm por uma VARIAVEL de
        # mesmo nome: sem a variavel, o parametro sozinho nao resolve o token
        parsed = yaml.safe_load(
            next(
                f
                for f in build_bundle(
                    app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
                ).files
                if f.path == "azure-pipelines.yaml"
            ).content
        )
        variaveis = {v["name"]: v["value"] for v in parsed["variables"] if "name" in v}
        assert variaveis["imageTag"] == "$(Build.SourceBranchName)-$(Build.BuildId)"
        assert variaveis["SQ_PROJECT_KEY"] == "$(Build.Repository.Name)"
        assert parsed["extends"]["parameters"]["imageTag"] == "$(imageTag)"

    def test_params_api(self, base_ctx: dict[str, object]) -> None:
        params = self._params(
            app_type=AppType.API,
            python_version="3.11",
            python_package_manager="poetry",
            application_name="api-utils",
            apim_path="utils",
            api_ids=dict.fromkeys(("dev", "qa", "uat", "prod"), "api-utils"),
            **base_ctx,
        )
        assert params["ApplicationType"] == "python"
        assert params["projectType"] == "api"
        assert params["pythonVersion"] == "3.11"
        assert params["packageManager"] == "poetry"
        assert params["applicationName"] == "api-utils"
        assert params["namespace"] == "payments-prod"
        assert params["deploymentStrategy"] == "rolling"
        assert params["enableApim"] is True
        assert params["pathApiManagementService"] == "utils"
        # apiId unico (o template python nao tem apiIdDev/Qa/Uat/Prod)
        assert params["apiId"] == "api-utils"
        assert not [k for k in params if k.startswith("apiId") and k != "apiId"]
        assert params["imageName"] == "$(Build.Repository.Name)"
        assert params["imageTag"] == "$(imageTag)"
        assert params["dockerfilePath"] == "Dockerfile"
        assert params["failOnVulnerabilities"] is False
        assert params["keyvault"] == ""
        # poetry nao usa requirementsFile (o build.yml resolve pelo poetry.lock)
        assert "requirementsFile" not in params
        # nenhum parametro do contrato dotnet vaza para o python
        assert "dotNetVersion" not in params

    def test_pip_emite_requirements_file(self, base_ctx: dict[str, object]) -> None:
        params = self._params(app_type=AppType.API, python_package_manager="pip", **base_ctx)
        assert params["requirementsFile"] == "requirements.txt"

    def test_quality_gates(self, base_ctx: dict[str, object]) -> None:
        # runTests desligado (repo sem pasta tests quebraria no primeiro run), mas com
        # testPath/minimumCoverage ja preenchidos para o time so trocar o flag
        params = self._params(app_type=AppType.API, **base_ctx)
        assert params["runTests"] is False
        assert params["testPath"] == "tests"
        assert params["testRequirementsFile"] == "requirements-dev.txt"
        assert params["minimumCoverage"] == 80
        assert params["runLint"] is True
        assert params["lintTool"] == "ruff"
        assert params["sourceFolder"] == "."

    @pytest.mark.parametrize(
        ("app_type", "project_type"),
        [(AppType.API, "api"), (AppType.WORKER, "worker"), (AppType.CRONJOB, "worker")],
    )
    def test_project_type_por_tipo(
        self, app_type: AppType, project_type: str, base_ctx: dict[str, object]
    ) -> None:
        # projectType do template so aceita api|worker; cronjob deploya como worker
        params = self._params(app_type=app_type, **base_ctx)
        assert params["projectType"] == project_type

    @pytest.mark.parametrize("app_type", [AppType.WORKER, AppType.CRONJOB])
    def test_apim_so_para_api(self, app_type: AppType, base_ctx: dict[str, object]) -> None:
        params = self._params(app_type=app_type, **base_ctx)
        assert params["enableApim"] is False
        assert "apiId" not in params
        assert "pathApiManagementService" not in params

    def test_web_python_cai_no_default(self, base_ctx: dict[str, object]) -> None:
        # web e sempre SPA em nginx: ignora a linguagem e usa o contrato generico
        params = self._params(app_type=AppType.WEB, **base_ctx)
        assert params["appType"] == "web"
        assert "ApplicationType" not in params


class TestDockerfilePython:
    """Modelo do time: multi-stage slim, pip install --user copiado para o usuario
    nao-root, ENVs de imagem no proprio Dockerfile e porta 8000."""

    def test_builda_do_codigo_fonte_com_pip(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        # a pipeline python nao publica artefato (downloadArtifact: false): o install
        # das dependencias acontece na imagem, em estagio de build separado
        assert "AS builder" in df
        assert "build-essential" in df
        assert "COPY requirements.txt ." in df
        assert "pip install --no-cache-dir --user -r requirements.txt" in df
        assert "COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local" in df
        assert "poetry" not in df

    def test_roda_como_usuario_nao_root(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "RUN useradd --create-home --shell /bin/bash appuser" in df
        assert "USER appuser" in df
        assert "COPY --chown=appuser:appuser . ." in df
        assert "RUN mkdir -p /app/data /app/temp /app/logs" in df

    def test_envs_de_imagem_no_dockerfile(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        envs = [ln.strip() for ln in df.splitlines() if ln.startswith("ENV ")]
        assert envs == [
            "ENV PATH=/home/appuser/.local/bin:$PATH",
            "ENV PYTHONDONTWRITEBYTECODE=1",
            "ENV PYTHONUNBUFFERED=1",
            "ENV PYTHONPATH=/app",
        ]

    def test_sem_libs_de_imagem_video_por_padrao(self, base_ctx: dict[str, object]) -> None:
        # libgl1/libglib2.0-0 sao deps de OpenCV/Pillow: quem precisar acrescenta
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "libgl1" not in df
        assert "libglib" not in df

    def test_poetry_exporta_o_lock(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.PYTHON,
            python_package_manager="poetry",
            **base_ctx,  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "COPY pyproject.toml poetry.lock* ./" in df
        assert "poetry export" in df
        # tambem instala com --user, para o COPY do estagio de runtime funcionar igual
        assert "pip install --no-cache-dir --user -r requirements.txt" in df
        assert "COPY requirements.txt ." not in df

    def test_versao_define_a_tag_da_imagem(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.PYTHON,
            python_version="3.11",
            **base_ctx,  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert df.count("FROM python:3.11-slim") == 2
        assert "alpine" not in df

    def test_cmd_em_exec_form(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.PYTHON,
            python_entrypoint="gunicorn app.main:app --bind 0.0.0.0:8080",
            **base_ctx,  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert 'CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:8080"]' in df

    @pytest.mark.parametrize(
        ("app_type", "cmd"),
        [
            (AppType.API, 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]'),
            (AppType.WORKER, 'CMD ["python", "main.py"]'),
            (AppType.CRONJOB, 'CMD ["python", "main.py"]'),
        ],
    )
    def test_cmd_default_por_tipo(
        self, app_type: AppType, cmd: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=app_type, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert cmd in df

    @pytest.mark.parametrize("app_type", [AppType.WORKER, AppType.CRONJOB])
    def test_sem_expose_nem_healthcheck_fora_de_api(
        self, app_type: AppType, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=app_type, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "EXPOSE" not in df
        assert "HEALTHCHECK" not in df

    @pytest.mark.parametrize(
        ("app_type", "chave"),
        [(AppType.API, "microservice-chart"), (AppType.WORKER, "worker-chart")],
    )
    def test_values_recebem_so_o_que_descreve_o_ambiente(
        self, app_type: AppType, chave: str, base_ctx: dict[str, object]
    ) -> None:
        # PYTHON*/PATH descrevem a imagem e ficam no Dockerfile (modelo do time);
        # o values leva o que varia por ambiente
        bundle = build_bundle(
            app_type=app_type, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        ev = next(f for f in bundle.files if f.path == "helm/environments/qa/values.yaml")
        app_env = yaml.safe_load(ev.content)[chave]["env"]
        assert app_env["TZ"] == "America/Sao_Paulo"
        assert "PYTHONUNBUFFERED" not in app_env
        assert "PYTHONPATH" not in app_env
        assert "ASPNETCORE_ENVIRONMENT" not in app_env


class TestPortaPorLinguagem:
    """A porta atravessa Dockerfile, service e ingresses - tem que casar nos tres."""

    def test_python_api_usa_8000_ponta_a_ponta(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "EXPOSE 8000" in df
        assert "--port 8000" in df.replace('", "', " ")

        base_values = next(f for f in bundle.files if f.path == "helm/base/values.yaml")
        assert yaml.safe_load(base_values.content)["microservice-chart"]["service"]["port"] == 8000

        ev = next(f for f in bundle.files if f.path == "helm/environments/prod/values.yaml")
        for ingress in yaml.safe_load(ev.content)["microservice-chart"]["ingresses"]:
            assert ingress["rules"][0]["paths"][0]["port"] == 8000

    def test_dotnet_api_continua_em_8080(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.DOTNET, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "EXPOSE 8080" in df
        base_values = next(f for f in bundle.files if f.path == "helm/base/values.yaml")
        assert yaml.safe_load(base_values.content)["microservice-chart"]["service"]["port"] == 8080
        ev = next(f for f in bundle.files if f.path == "helm/environments/prod/values.yaml")
        for ingress in yaml.safe_load(ev.content)["microservice-chart"]["ingresses"]:
            assert ingress["rules"][0]["paths"][0]["port"] == 8080
        # a ENV do runtime .NET aponta para a mesma porta
        app_env = yaml.safe_load(ev.content)["microservice-chart"]["env"]
        assert app_env["ASPNETCORE_URLS"] == "http://+:8080"


class TestRenderedContent:
    def test_pipeline_dotnet_estrutura_do_modelo_do_time(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        # default language=dotnet -> usa o template de pipeline dotnet (modelo real do time)
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["name"] == "$(Build.SourceBranchName).$(Build.BuildId)"
        assert parsed["trigger"]["branches"]["include"] == [
            "develop",
            "release/*",
            "hotfix/*",
            "feature/*",
        ]
        repos = {r["repository"]: r for r in parsed["resources"]["repositories"]}
        # templates referenciado por branch (contrato template-aks usa refs/heads/master)
        assert repos["templates"]["ref"] == "refs/heads/master"
        assert repos["templates"]["name"] == "Platform/devops-templates"
        # o Helm vive no proprio repo da app: sem resource manifests
        assert "manifests" not in repos
        assert parsed["pool"]["name"] == "agt_pool_linux_vmss"
        var_groups = [v["group"] for v in parsed["variables"] if "group" in v]
        assert "Sonarqube" in var_groups and "k8s-prod" in var_groups
        # alias @templates
        assert parsed["extends"]["template"] == "pipelines/k8s-app.yaml@templates"

    def test_pipeline_dotnet_ref_branch_configuravel(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, devops_templates_branch="release/aks", **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        repos = {
            r["repository"]: r
            for r in yaml.safe_load(pipeline.content)["resources"]["repositories"]
        }
        assert repos["templates"]["ref"] == "refs/heads/release/aks"

    def test_pipeline_dotnet_params(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            dotnet_version="9.0.x",
            application_name="api-utils",
            apim_path="utils",
            api_ids={"dev": "utils-dev", "qa": "utils-qa", "uat": "utils-uat", "prod": "utils"},
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        params = yaml.safe_load(pipeline.content)["extends"]["parameters"]
        assert params["applicationName"] == "api-utils"
        assert params["namespace"] == "payments-prod"
        assert params["ApplicationType"] == "dotnet"
        assert params["projectType"] == "webapi"
        assert params["dotNetVersion"] == "9.0.x"
        # contrato template-aks: enableApim no lugar de deployAKS/deployAppService
        assert params["enableApim"] is True
        assert "deployAKS" not in params
        assert "deployAppService" not in params
        assert "pathAutomation" not in params
        assert params["deploymentStrategy"] == "rolling"
        assert params["pathApiManagementService"] == "utils"
        # apiIds (template-aks): obrigatorios, um por ambiente, emitidos para api
        assert params["apiIdDev"] == "utils-dev"
        assert params["apiIdQa"] == "utils-qa"
        assert params["apiIdUat"] == "utils-uat"
        assert params["apiIdProd"] == "utils"
        assert "apiId" not in params
        # imageName = macro do repo (modelo real do time)
        assert params["imageName"] == "$(Build.Repository.Name)"
        assert params["imageTag"] == "$(Build.SourceBranchName)-$(Build.BuildId)"
        assert "appType" not in params

    def test_pipeline_apiids_so_para_api(self, base_ctx: dict[str, object]) -> None:
        # os apiIds do APIM so sao emitidos para api; worker/cronjob dotnet nao passam nenhum
        api = build_bundle(
            app_type=AppType.API,
            api_ids=dict.fromkeys(("dev", "qa", "uat", "prod"), "api-utils"),
            **base_ctx,  # type: ignore[arg-type]
        )
        worker = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        api_params = yaml.safe_load(
            next(f for f in api.files if f.path == "azure-pipelines.yaml").content
        )["extends"]["parameters"]
        worker_params = yaml.safe_load(
            next(f for f in worker.files if f.path == "azure-pipelines.yaml").content
        )["extends"]["parameters"]
        # valor unico informado no fluxo se repete nos quatro parametros
        assert api_params["apiIdDev"] == "api-utils"
        assert api_params["apiIdQa"] == "api-utils"
        assert api_params["apiIdUat"] == "api-utils"
        assert api_params["apiIdProd"] == "api-utils"
        assert not [k for k in worker_params if k.startswith("apiId")]

    def test_pipeline_dotnet_defaults_derivados(self, base_ctx: dict[str, object]) -> None:
        # sem application_name/apim_path informados: applicationName = app_name e
        # pathApiManagementService = app_name sem o prefixo 'api-'
        ctx = {**base_ctx, "app_name": "api-authentication"}
        bundle = build_bundle(app_type=AppType.API, **ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        params = yaml.safe_load(pipeline.content)["extends"]["parameters"]
        assert params["applicationName"] == "api-authentication"
        assert params["pathApiManagementService"] == "authentication"

    def test_pipeline_worker_dotnet_desativa_apim(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        params = yaml.safe_load(pipeline.content)["extends"]["parameters"]
        assert params["enableApim"] is False

    def test_pipeline_default_para_linguagem_nao_dotnet(
        self, base_ctx: dict[str, object]
    ) -> None:
        # go (nao-dotnet) cai no template _default, que mantem o contrato generico
        bundle = build_bundle(
            app_type=AppType.API, language=Language.GO, **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["parameters"]["appType"] == "api"
        assert parsed["extends"]["parameters"]["exposeExternal"] is False

    def test_pipeline_default_usa_template_default(self, base_ctx: dict[str, object]) -> None:
        # _default (linguagem nao-dotnet) referencia o template por @devops-templates
        bundle = build_bundle(
            app_type=AppType.API, language=Language.GO, **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["template"] == "pipelines/k8s-app.yaml@devops-templates"

    def test_pipeline_default_usa_template_informado(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.GO,
            pipeline_template_path="pipelines/go-k8s-app.yaml",
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["template"] == "pipelines/go-k8s-app.yaml@devops-templates"

    def test_pipeline_default_helm_chart_path_helm_base(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.GO, **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["parameters"]["helmChartPath"] == "helm/base"

    def test_pipeline_nao_passa_language_por_padrao(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert "language" not in parsed["extends"]["parameters"]

    def test_pipeline_passa_language_quando_habilitado(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.GO,
            pipeline_pass_language=True,
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["parameters"]["language"] == "go"

    def test_pipeline_web_nao_passa_language_mesmo_habilitado(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.WEB,
            pipeline_pass_language=True,
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert "language" not in parsed["extends"]["parameters"]

    def test_chart_yaml_e_yaml_valido(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        chart = next(f for f in bundle.files if f.path == "helm/base/Chart.yaml")
        parsed = yaml.safe_load(chart.content)
        assert parsed["name"] == "payments-api"
        assert parsed["apiVersion"] == "v2"

    def test_values_base_microservice_api_web(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.files if f.path == "helm/base/values.yaml"
        )
        mc = yaml.safe_load(values.content)["microservice-chart"]
        assert mc["strategy"]["type"] == "RollingUpdate"
        assert mc["service"]["port"] == 8080
        assert mc["autoscaling"]["enabled"] is False
        assert mc["pdb"]["enabled"] is False

    def test_values_base_worker(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.files if f.path == "helm/base/values.yaml"
        )
        wc = yaml.safe_load(values.content)["worker-chart"]
        assert wc["strategy"]["type"] == "Recreate"
        assert "service" not in wc
        assert wc["autoscaling"]["enabled"] is True

    def test_values_yaml_cronjob_tem_schedule_sem_service_nem_autoscaling(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.files if f.path == "helm/base/values.yaml"
        )
        parsed = yaml.safe_load(values.content)
        assert "service" not in parsed
        assert "ingress" not in parsed
        assert "probes" not in parsed
        assert "autoscaling" not in parsed
        assert "replicaCount" not in parsed
        assert parsed["cronjob"]["schedule"] == "0 * * * *"
        assert parsed["cronjob"]["concurrencyPolicy"] == "Forbid"
        assert parsed["cronjob"]["restartPolicy"] == "OnFailure"

    def test_values_yaml_cronjob_usa_schedule_default(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.files if f.path == "helm/base/values.yaml"
        )
        parsed = yaml.safe_load(values.content)
        assert parsed["cronjob"]["schedule"] == "0 * * * *"

    def test_values_yaml_cronjob_usa_schedule_informado(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.CRONJOB, schedule="30 3 * * 1", **base_ctx  # type: ignore[arg-type]
        )
        values = next(
            f for f in bundle.files if f.path == "helm/base/values.yaml"
        )
        parsed = yaml.safe_load(values.content)
        assert parsed["cronjob"]["schedule"] == "30 3 * * 1"

    def test_cronjob_yaml_e_template_de_cronjob_kubernetes(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        cron = next(f for f in bundle.files if f.path.endswith("cronjob.yaml"))
        assert "kind: CronJob" in cron.content
        assert "apiVersion: batch/v1" in cron.content
        assert ".Values.cronjob.schedule" in cron.content

    def test_dockerfile_api_expose_porta_8080(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "EXPOSE 8080" in df.content

    def test_dockerfile_dotnet_instala_icu(self, base_ctx: dict[str, object]) -> None:
        # modelo do time: icu-libs no runtime para culturas/ICU (a variavel de
        # globalizacao vive no values de cada ambiente, nao no Dockerfile)
        bundle = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "icu-libs" in df

    def test_dockerfile_worker_sem_expose(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content

    def test_dockerfile_web_usa_nginx(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WEB, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "nginx" in df.content
        assert 'CMD ["nginx", "-g", "daemon off;"]' in df.content

    def test_dockerfile_cronjob_sem_expose_nem_healthcheck(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content
        assert "HEALTHCHECK" not in df.content


class TestLanguageDockerfile:
    @pytest.mark.parametrize(
        ("language", "marker"),
        [
            (Language.DOTNET, "dotnet/aspnet:"),
            (Language.NODE, "node:20-alpine"),
            (Language.GO, "alpine:3.20"),
        ],
    )
    def test_api_usa_template_da_linguagem(
        self, language: Language, marker: str, base_ctx: dict[str, object]
    ) -> None:
        # python tem modelo proprio (multi-stage, slim, porta 8000) - ver TestDockerfilePython
        bundle = build_bundle(app_type=AppType.API, language=language, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert marker in df.content
        assert "EXPOSE 8080" in df.content
        # runtime-only: a imagem so roda o artefato (sem build stage)
        assert "COPY . ." in df.content
        assert "America/Sao_Paulo" in df.content

    def test_api_python_usa_o_modelo_do_time(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.PYTHON, **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "python:3.12-slim" in df
        assert "EXPOSE 8000" in df

    @pytest.mark.parametrize("language", list(Language))
    def test_web_ignora_linguagem_e_usa_nginx(
        self, language: Language, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.WEB, language=language, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "nginx" in df.content
        assert "dotnet" not in df.content
        assert "go build" not in df.content

    def test_dotnet_troca_aspnet_por_runtime_conforme_tipo(
        self, base_ctx: dict[str, object]
    ) -> None:
        api = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        worker = build_bundle(app_type=AppType.WORKER, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        api_df = next(f for f in api.files if f.path == "Dockerfile").content
        worker_df = next(f for f in worker.files if f.path == "Dockerfile").content
        assert "dotnet/aspnet" in api_df
        assert "dotnet/runtime" in worker_df
        assert "dotnet/aspnet" not in worker_df

    def test_dotnet_image_tag_da_versao(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.DOTNET, dotnet_version="9.0.x", **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.files if f.path == "Dockerfile").content
        assert "dotnet/aspnet:9.0-alpine" in df

    def test_dotnet_entrypoint_informado_e_default(self, base_ctx: dict[str, object]) -> None:
        # build_bundle usa o entrypoint como recebido (a normalizacao .dll e da validacao)
        informado = build_bundle(
            app_type=AppType.API,
            language=Language.DOTNET,
            dotnet_entrypoint="ePharma.Auth.Services.dll",
            **base_ctx,  # type: ignore[arg-type]
        )
        df_info = next(f for f in informado.files if f.path == "Dockerfile").content
        assert 'ENTRYPOINT ["dotnet", "ePharma.Auth.Services.dll"]' in df_info

        # sem entrypoint -> default {app_name}.dll
        default = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        df_def = next(f for f in default.files if f.path == "Dockerfile").content
        assert 'ENTRYPOINT ["dotnet", "payments-api.dll"]' in df_def

    def test_node_worker_sem_expose(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, language=Language.NODE, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content
        assert "node" in df.content

    def test_go_cronjob_sem_expose_nem_healthcheck(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, language=Language.GO, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content
        assert "HEALTHCHECK" not in df.content


class TestDryRun:
    def test_sem_colisoes(self, base_ctx: dict[str, object]) -> None:
        r = dry_run(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        assert r.ok
        assert r.data is not None
        assert not r.data.has_collisions

    def test_detecta_colisao_no_repo_da_app(self, base_ctx: dict[str, object]) -> None:
        r = dry_run(
            app_type=AppType.API,
            existing_repo_paths={"Dockerfile", "src/main.cs"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert "Dockerfile" in r.data.collisions
        assert r.data.has_collisions

    def test_detecta_colisao_nos_arquivos_do_helm(self, base_ctx: dict[str, object]) -> None:
        r = dry_run(
            app_type=AppType.API,
            existing_repo_paths={"helm/base/Chart.yaml"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert "helm/base/Chart.yaml" in r.data.collisions

    def test_normalizacao_de_path_evita_falso_negativo(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        r = dry_run(
            app_type=AppType.API,
            existing_repo_paths={"./Dockerfile"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert r.data.collisions == ["Dockerfile"]
