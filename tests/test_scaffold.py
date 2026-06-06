"""Testes da tool de scaffold - garantem que templates renderizam consistentemente."""

from __future__ import annotations

import base64
import io
import tarfile

import pytest
import yaml

from devops_agent.models import AppType, Language
from devops_agent.tools.scaffold import _pipeline_stack, build_bundle, dry_run


class TestPipelineStack:
    def test_dotnet_nao_web_usa_stack_dotnet(self) -> None:
        assert _pipeline_stack(AppType.API, Language.DOTNET) == "dotnet"
        assert _pipeline_stack(AppType.CRONJOB, Language.DOTNET) == "dotnet"

    def test_nao_dotnet_usa_default(self) -> None:
        assert _pipeline_stack(AppType.API, Language.GO) == "_default"
        assert _pipeline_stack(AppType.WORKER, Language.NODE) == "_default"

    def test_web_sempre_default(self) -> None:
        # web ignora a linguagem; nao usa o stack dotnet
        assert _pipeline_stack(AppType.WEB, Language.DOTNET) == "_default"

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
    def test_sempre_contem_dockerfile_e_pipeline_no_repo_da_app(
        self,
        app_type: AppType,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = bundle.all_app_paths
        assert "Dockerfile" in paths
        assert "azure-pipelines.yaml" in paths
        assert len(paths) == 2

    @pytest.mark.parametrize("app_type", [AppType.API, AppType.WEB, AppType.WORKER])
    def test_base_chart_apps_sao_charts_finas_com_dependency(
        self,
        app_type: AppType,
        base_ctx: dict[str, object],
    ) -> None:
        # api/web/worker: chart fina (Chart.yaml + values.yaml), SEM templates proprios
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_helm_paths)
        chart_root = "helm/apps/payments-api"
        assert f"{chart_root}/Chart.yaml" in paths
        assert f"{chart_root}/values.yaml" in paths
        assert not any(p.startswith(f"{chart_root}/templates/") for p in paths)
        # Chart.yaml declara dependency na base chart do time
        chart = next(f for f in bundle.helm_chart_files if f.path.endswith("/Chart.yaml"))
        dep = yaml.safe_load(chart.content)["dependencies"][0]
        assert dep["name"] in ("microservice-chart", "worker-chart")
        assert "AksHelmBaseCharts/base-charts/" in dep["repository"]

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
        chart = next(f for f in bundle.helm_chart_files if f.path.endswith("/Chart.yaml"))
        assert yaml.safe_load(chart.content)["dependencies"][0]["name"] == base_chart

    def test_cronjob_e_self_contained(self, base_ctx: dict[str, object]) -> None:
        # cronjob nao tem base chart -> mantem o chart self-contained com templates
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_helm_paths)
        chart_root = "helm/apps/payments-api"
        assert f"{chart_root}/templates/cronjob.yaml" in paths
        assert f"{chart_root}/templates/_helpers.tpl" in paths
        assert f"{chart_root}/templates/serviceaccount.yaml" in paths


class TestVendoredBaseChart:
    """A chart fina vendoriza a base chart como charts/<base>-<ver>.tgz + Chart.lock,
    igual ao modelo do time (AksHelmCharts), para que `helm` ache as dependencias."""

    @pytest.mark.parametrize(
        ("app_type", "tgz"),
        [
            (AppType.API, "microservice-chart-0.4.4.tgz"),
            (AppType.WEB, "microservice-chart-0.4.4.tgz"),
            (AppType.WORKER, "worker-chart-1.0.0.tgz"),
        ],
    )
    def test_emite_tgz_e_chart_lock(
        self, app_type: AppType, tgz: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=app_type, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_helm_paths)
        chart_root = "helm/apps/payments-api"
        assert f"{chart_root}/charts/{tgz}" in paths
        assert f"{chart_root}/Chart.lock" in paths

    def test_tgz_e_binario_e_contem_templates_da_base(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        tgz = next(f for f in bundle.helm_chart_files if f.path.endswith(".tgz"))
        assert tgz.is_binary
        assert tgz.content == ""
        raw = base64.b64decode(tgz.binary_b64 or "")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            names = tar.getnames()
        # o .tgz embala os manifests reais da base chart (deployment/service/ingress)
        assert "microservice-chart/templates/deployment.yaml" in names
        assert "microservice-chart/templates/service.yaml" in names
        assert "microservice-chart/templates/ingress.yaml" in names

    def test_chart_lock_casa_com_a_dependency_do_chart_yaml(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        lock = next(f for f in bundle.helm_chart_files if f.path.endswith("/Chart.lock"))
        chart = next(f for f in bundle.helm_chart_files if f.path.endswith("/Chart.yaml"))
        lock_dep = yaml.safe_load(lock.content)["dependencies"][0]
        chart_dep = yaml.safe_load(chart.content)["dependencies"][0]
        assert lock_dep["name"] == chart_dep["name"] == "worker-chart"
        assert lock_dep["version"] == chart_dep["version"] == "1.0.0"
        assert yaml.safe_load(lock.content)["digest"].startswith("sha256:")

    def test_cronjob_nao_vendoriza_tgz(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        assert not any(f.path.endswith(".tgz") for f in bundle.helm_chart_files)
        assert not any(f.path.endswith("/Chart.lock") for f in bundle.helm_chart_files)


class TestEnvironments:
    def test_gera_override_por_ambiente_default(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        paths = set(bundle.all_helm_paths)
        for env in ("dev", "qa", "uat", "prod"):
            assert f"helm/environments/{env}/payments-api/values.yaml" in paths

    def test_environments_customizado(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, environments=["dev", "prod"], **base_ctx  # type: ignore[arg-type]
        )
        paths = set(bundle.all_helm_paths)
        assert "helm/environments/dev/payments-api/values.yaml" in paths
        assert "helm/environments/prod/payments-api/values.yaml" in paths
        assert "helm/environments/qa/payments-api/values.yaml" not in paths

    def test_override_microservice_skeleton(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        ev = next(
            f for f in bundle.helm_chart_files
            if f.path == "helm/environments/prod/payments-api/values.yaml"
        )
        mc = yaml.safe_load(ev.content)["microservice-chart"]
        # registry por ambiente + tag placeholder substituida pela pipeline
        assert mc["image"]["repository"] == "acraksprodeastus.azurecr.io/payments-api"
        assert mc["image"]["tag"] == "__imageTag__"
        assert mc["environment"] == "prod"
        # api/web tem ingresses no override
        assert mc["ingresses"][0]["name"] == "payments-api"
        assert mc["serviceAccount"]["name"] == "sa-payments-api"

    def test_override_worker_sem_ingresses(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        ev = next(
            f for f in bundle.helm_chart_files
            if f.path == "helm/environments/dev/payments-api/values.yaml"
        )
        wc = yaml.safe_load(ev.content)["worker-chart"]
        assert wc["image"]["repository"] == "acraksdeveastus.azurecr.io/payments-api"
        assert "ingresses" not in wc


class TestRenderedContent:
    def test_pipeline_dotnet_estrutura_do_modelo_do_time(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        # default language=dotnet -> usa o template de pipeline dotnet (modelo real do time)
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["name"] == "$(Build.SourceBranchName).$(Build.BuildId)"
        assert parsed["trigger"]["branches"]["include"] == [
            "develop",
            "release/*",
            "hotfix/*",
            "feature/*",
        ]
        repos = {r["repository"]: r for r in parsed["resources"]["repositories"]}
        # templates SEM ref (usa branch default do repo, nao tag)
        assert "ref" not in repos["templates"]
        assert repos["templates"]["name"] == "Platform/devops-templates"
        # manifests = repo do Helm, referenciado por branch
        assert repos["manifests"]["ref"] == "refs/heads/main"
        assert parsed["pool"]["name"] == "agt_pool_linux_vmss"
        var_groups = [v["group"] for v in parsed["variables"] if "group" in v]
        assert "Sonarqube" in var_groups and "k8s-prod" in var_groups
        # alias @templates, sem tag
        assert parsed["extends"]["template"] == "pipelines/k8s-app.yaml@templates"

    def test_pipeline_dotnet_manifests_usa_repo_e_branch_configurados(
        self, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            devops_helm_repo_name="Platform/AksHelmCharts",
            devops_helm_repo_branch="master",
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        repos = {
            r["repository"]: r
            for r in yaml.safe_load(pipeline.content)["resources"]["repositories"]
        }
        assert repos["manifests"]["name"] == "Platform/AksHelmCharts"
        assert repos["manifests"]["ref"] == "refs/heads/master"

    def test_pipeline_dotnet_params(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, dotnet_version="9.0.x", **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        params = yaml.safe_load(pipeline.content)["extends"]["parameters"]
        assert params["applicationName"] == "payments-api"
        assert params["namespace"] == "payments-prod"
        assert params["ApplicationType"] == "dotnet"
        assert params["projectType"] == "webapi"
        assert params["dotNetVersion"] == "9.0.x"
        assert params["deployAKS"] is True
        assert params["deployAppService"] is False
        assert params["deploymentStrategy"] == "rolling"
        assert params["imageName"] == "$(Build.Repository.Name)"
        assert params["imageTag"] == "$(Build.SourceBranchName)-$(Build.BuildId)"
        assert "appType" not in params

    def test_pipeline_dotnet_app_path_remove_prefixo_api(
        self, base_ctx: dict[str, object]
    ) -> None:
        ctx = {**base_ctx, "app_name": "api-authentication"}
        bundle = build_bundle(app_type=AppType.API, **ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        params = yaml.safe_load(pipeline.content)["extends"]["parameters"]
        assert params["pathApiManagementService"] == "authentication"
        assert params["pathAutomation"] == "authentication"

    def test_pipeline_default_para_linguagem_nao_dotnet(
        self, base_ctx: dict[str, object]
    ) -> None:
        # go (nao-dotnet) cai no template _default, que mantem o contrato generico
        bundle = build_bundle(
            app_type=AppType.API, language=Language.GO, **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["parameters"]["appType"] == "api"
        assert parsed["extends"]["parameters"]["exposeExternal"] is False

    def test_pipeline_default_usa_template_default(self, base_ctx: dict[str, object]) -> None:
        # _default (linguagem nao-dotnet) referencia o template por @devops-templates
        bundle = build_bundle(
            app_type=AppType.API, language=Language.GO, **base_ctx  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["template"] == "pipelines/k8s-app.yaml@devops-templates"

    def test_pipeline_default_usa_template_informado(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API,
            language=Language.GO,
            pipeline_template_path="pipelines/go-k8s-app.yaml",
            **base_ctx,  # type: ignore[arg-type]
        )
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert parsed["extends"]["template"] == "pipelines/go-k8s-app.yaml@devops-templates"

    def test_pipeline_nao_passa_language_por_padrao(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
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
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
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
        pipeline = next(f for f in bundle.app_repo_files if f.path == "azure-pipelines.yaml")
        parsed = yaml.safe_load(pipeline.content)
        assert "language" not in parsed["extends"]["parameters"]

    def test_chart_yaml_e_yaml_valido(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        chart = next(f for f in bundle.helm_chart_files if f.path.endswith("Chart.yaml"))
        parsed = yaml.safe_load(chart.content)
        assert parsed["name"] == "payments-api"
        assert parsed["apiVersion"] == "v2"

    def test_values_base_microservice_api_web(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.helm_chart_files if f.path == "helm/apps/payments-api/values.yaml"
        )
        mc = yaml.safe_load(values.content)["microservice-chart"]
        assert mc["strategy"]["type"] == "RollingUpdate"
        assert mc["service"]["port"] == 8080
        assert mc["autoscaling"]["enabled"] is False
        assert mc["pdb"]["enabled"] is False

    def test_values_base_worker(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        values = next(
            f for f in bundle.helm_chart_files if f.path == "helm/apps/payments-api/values.yaml"
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
            f for f in bundle.helm_chart_files if f.path == "helm/apps/payments-api/values.yaml"
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
            f for f in bundle.helm_chart_files if f.path == "helm/apps/payments-api/values.yaml"
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
            f for f in bundle.helm_chart_files if f.path == "helm/apps/payments-api/values.yaml"
        )
        parsed = yaml.safe_load(values.content)
        assert parsed["cronjob"]["schedule"] == "30 3 * * 1"

    def test_cronjob_yaml_e_template_de_cronjob_kubernetes(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        cron = next(f for f in bundle.helm_chart_files if f.path.endswith("cronjob.yaml"))
        assert "kind: CronJob" in cron.content
        assert "apiVersion: batch/v1" in cron.content
        assert ".Values.cronjob.schedule" in cron.content

    def test_dockerfile_api_expose_porta_8080(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.API, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "EXPOSE 8080" in df.content

    def test_dockerfile_worker_sem_expose(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content

    def test_dockerfile_web_usa_nginx(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WEB, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "nginx" in df.content
        assert 'CMD ["nginx", "-g", "daemon off;"]' in df.content

    def test_dockerfile_cronjob_sem_expose_nem_healthcheck(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content
        assert "HEALTHCHECK" not in df.content


class TestLanguageDockerfile:
    @pytest.mark.parametrize(
        ("language", "marker"),
        [
            (Language.DOTNET, "dotnet/aspnet:"),
            (Language.NODE, "node:20-alpine"),
            (Language.PYTHON, "python:3.12-alpine"),
            (Language.GO, "alpine:3.20"),
        ],
    )
    def test_api_usa_template_da_linguagem(
        self, language: Language, marker: str, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.API, language=language, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert marker in df.content
        # API sempre expoe porta, independente da linguagem
        assert "EXPOSE 8080" in df.content
        # runtime-only: a imagem so roda o artefato (sem build stage)
        assert "COPY . ." in df.content
        assert "America/Sao_Paulo" in df.content

    @pytest.mark.parametrize("language", list(Language))
    def test_web_ignora_linguagem_e_usa_nginx(
        self, language: Language, base_ctx: dict[str, object]
    ) -> None:
        bundle = build_bundle(app_type=AppType.WEB, language=language, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "nginx" in df.content
        assert "dotnet" not in df.content
        assert "go build" not in df.content

    def test_dotnet_troca_aspnet_por_runtime_conforme_tipo(
        self, base_ctx: dict[str, object]
    ) -> None:
        api = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        worker = build_bundle(app_type=AppType.WORKER, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        api_df = next(f for f in api.app_repo_files if f.path == "Dockerfile").content
        worker_df = next(f for f in worker.app_repo_files if f.path == "Dockerfile").content
        assert "dotnet/aspnet" in api_df
        assert "dotnet/runtime" in worker_df
        assert "dotnet/aspnet" not in worker_df

    def test_dotnet_image_tag_da_versao(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(
            app_type=AppType.API, language=Language.DOTNET, dotnet_version="9.0.x", **base_ctx  # type: ignore[arg-type]
        )
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile").content
        assert "dotnet/aspnet:9.0-alpine" in df

    def test_dotnet_entrypoint_informado_e_default(self, base_ctx: dict[str, object]) -> None:
        # build_bundle usa o entrypoint como recebido (a normalizacao .dll e da validacao)
        informado = build_bundle(
            app_type=AppType.API,
            language=Language.DOTNET,
            dotnet_entrypoint="ePharma.Auth.Services.dll",
            **base_ctx,  # type: ignore[arg-type]
        )
        df_info = next(f for f in informado.app_repo_files if f.path == "Dockerfile").content
        assert 'ENTRYPOINT ["dotnet", "ePharma.Auth.Services.dll"]' in df_info

        # sem entrypoint -> default {app_name}.dll
        default = build_bundle(app_type=AppType.API, language=Language.DOTNET, **base_ctx)  # type: ignore[arg-type]
        df_def = next(f for f in default.app_repo_files if f.path == "Dockerfile").content
        assert 'ENTRYPOINT ["dotnet", "payments-api.dll"]' in df_def

    def test_node_worker_sem_expose(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.WORKER, language=Language.NODE, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
        assert "EXPOSE" not in df.content
        assert "node" in df.content

    def test_go_cronjob_sem_expose_nem_healthcheck(self, base_ctx: dict[str, object]) -> None:
        bundle = build_bundle(app_type=AppType.CRONJOB, language=Language.GO, **base_ctx)  # type: ignore[arg-type]
        df = next(f for f in bundle.app_repo_files if f.path == "Dockerfile")
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
            existing_app_repo_paths={"Dockerfile", "src/main.cs"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert "Dockerfile" in r.data.app_repo_collisions
        assert r.data.has_collisions

    def test_detecta_colisao_no_repo_helm(self, base_ctx: dict[str, object]) -> None:
        r = dry_run(
            app_type=AppType.API,
            existing_helm_repo_paths={"helm/apps/payments-api/Chart.yaml"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert "helm/apps/payments-api/Chart.yaml" in r.data.helm_repo_collisions

    def test_normalizacao_de_path_evita_falso_negativo(
        self,
        base_ctx: dict[str, object],
    ) -> None:
        r = dry_run(
            app_type=AppType.API,
            existing_app_repo_paths={"./Dockerfile"},
            **base_ctx,  # type: ignore[arg-type]
        )
        assert r.ok
        assert r.data is not None
        assert r.data.app_repo_collisions == ["Dockerfile"]
