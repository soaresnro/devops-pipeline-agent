"""Smoke test do CLI conduzindo o fluxo conversacional via stdin.

Valida que as 4 perguntas, validacoes, dry_run e CONFIRM funcionam ponta-a-ponta.
Para evitar dependencia de Azure DevOps real, o teste responde 'nao' no CONFIRM,
encerrando a sessao antes do EXECUTE.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run_cli(
    env_file_dir: Path, stdin_text: str, command: str = "scaffold"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "devops_agent.cli", command, "--requested-by", "tester@local"],
        cwd=str(env_file_dir),
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cli_conduz_fluxo_completo_ate_confirm(tmp_path: Path) -> None:
    """Roda as 4 perguntas, valida, gera dry_run e aborta no CONFIRM."""
    # Setup .env minimo
    # Sem AZDO_PAT: a deteccao de linguagem nao faz chamada de rede e cai em ASK_LANGUAGE,
    # mantendo o e2e offline e deterministico.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "AZDO_AUTH_MODE=pat\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n"
        "DEVOPS_TEMPLATES_TAG=v1\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            "main",
            "payments-prod",
            "api",
            "dotnet",  # responde a pergunta de linguagem (deteccao inconclusiva sem PAT)
            "",  # versao .NET: enter mantem o default 8.0.x
            "",  # entrypoint .NET: enter usa o default app_name.dll
            "",  # applicationName: enter usa o nome do repo
            "sim",  # usa APIM -> pergunta path e apiIds
            "",  # pathApiManagementService: enter deriva do applicationName
            "api-payments",  # apiIds: valor unico vale para dev/qa/uat/prod (obrigatorio)
            "",  # host do ingress: enter deriva de services<path>
            "nao",
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs)
    assert result.returncode == 2, f"esperado exit 2 (abort), saiu={result.returncode}\nstderr={result.stderr}\nstdout={result.stdout}"

    stdout = result.stdout
    assert "Qual o link do repositorio" in stdout
    assert "Em qual branch" in stdout
    assert "Qual o namespace" in stdout
    assert "API, Worker, Web ou CronJob" in stdout
    assert "linguagem" in stdout.lower()
    assert "Vou criar os seguintes arquivos" in stdout
    assert "Dockerfile" in stdout
    assert "azure-pipelines.yaml" in stdout
    assert "helm/base/Chart.yaml" in stdout
    assert "helm/base/charts/microservice-chart-0.4.5.tgz" in stdout
    assert "helm/environments/dev/values.yaml" in stdout
    assert "sessao cancelada" in stdout.lower()


def test_cli_chat_roda_fluxo_deterministico_sem_llm(tmp_path: Path) -> None:
    """chat e um alias deterministico de scaffold - nao exige Azure OpenAI."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "AZDO_AUTH_MODE=pat\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n"
        "DEVOPS_TEMPLATES_TAG=v1\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            "main",
            "payments-prod",
            "api",
            "dotnet",  # deteccao inconclusiva sem PAT -> pergunta a linguagem
            "",  # versao .NET: default
            "",  # entrypoint .NET: default
            "",  # applicationName: default
            "sim",  # usa APIM
            "",  # pathApiManagementService: default
            "api-payments",  # apiIds: valor unico vale para dev/qa/uat/prod (obrigatorio)
            "",  # host do ingress: default
            "nao",
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs, command="chat")
    assert result.returncode == 2, f"esperado exit 2 (abort), saiu={result.returncode}\nstdout={result.stdout}"
    assert "Modo deterministico" in result.stdout
    assert "AZURE_OPENAI" not in result.stdout
    assert "Qual o link do repositorio" in result.stdout
    assert "Vou criar os seguintes arquivos" in result.stdout
    assert "sessao cancelada" in result.stdout.lower()


def test_cli_apiids_por_virgula_e_reperguntados_se_quantidade_errada(tmp_path: Path) -> None:
    """Forma por virgula (4 valores) + repergunta quando a quantidade nao e 1 nem 4."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            "main",
            "payments-prod",
            "api",
            "dotnet",
            "",  # versao .NET
            "",  # entrypoint .NET
            "",  # applicationName
            "sim",  # usa APIM
            "",  # pathApiManagementService
            "api-dev,api-qa",  # so 2 valores -> repergunta
            "api-dev,api-qa,api-uat,api-payments",  # os quatro, na ordem
            "",  # host do ingress
            "nao",
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs)
    assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
    stdout = result.stdout
    assert "na ordem dev, qa, uat, prod" in stdout  # mensagem de erro da quantidade
    assert "apiIds: dev=api-dev, qa=api-qa, uat=api-uat, prod=api-payments" in stdout


def test_cli_fluxo_python_ate_confirm(tmp_path: Path) -> None:
    """Fluxo do stack python: versao + pip/poetry + entrypoint, e apiId unico."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n"
        "DEVOPS_TEMPLATE_PYTHON=pipelines/template-aks-python.yml\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/api-cotacao",
            "develop",
            "corporativo",
            "api",
            "python",  # deteccao inconclusiva sem PAT -> pergunta a linguagem
            "3.11",  # versao do Python
            "poetry",  # gerenciador de dependencias
            "uvicorn app.main:app --host 0.0.0.0 --port 8080",  # entrypoint
            "",  # applicationName: enter usa o nome do repo
            "sim",  # usa APIM -> pergunta path e apiIds
            "",  # pathApiManagementService: enter deriva do applicationName
            "c-dev,c-qa,c-uat,c-prod",  # apiId por ambiente, igual ao dotnet
            "",  # host do ingress: default
            "nao",
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs)
    assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
    stdout = result.stdout
    assert "versao do Python" in stdout
    assert "pip (requirements.txt) ou poetry" in stdout
    assert "exposta no APIM" in stdout
    assert "apiIds: dev=c-dev, qa=c-qa, uat=c-uat, prod=c-prod" in stdout
    assert "Vou criar os seguintes arquivos" in stdout
    assert "azure-pipelines.yaml" in stdout
    assert "helm/base/charts/microservice-chart-0.4.5.tgz" in stdout
    assert "sessao cancelada" in stdout.lower()


def test_cli_python_sem_apim_pula_path_e_apiid(tmp_path: Path) -> None:
    """Respondendo 'nao' ao APIM, o fluxo vai direto do applicationName ao ingress."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/api-cotacao",
            "develop",
            "corporativo",
            "api",
            "python",
            "",  # versao do Python
            "",  # gerenciador
            "",  # entrypoint
            "",  # applicationName
            "nao",  # sem APIM: nao pergunta path nem apiId
            "",  # host do ingress
            "nao",  # nao confirma o scaffold
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs)
    assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
    stdout = result.stdout
    assert "APIM: nao" in stdout
    # as fases de path e apiId nao chegam a rodar (os ecos delas nunca aparecem)
    assert "Path do APIM:" not in stdout
    assert "Qual o pathApiManagementService" not in stdout
    assert "Vou criar os seguintes arquivos" in stdout


def test_cli_rejeita_url_de_outra_org(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/outraorg/Platform/_git/payments-api",
            # Reperguntara - mando URL valida + segue
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            "main",
            "payments-prod",
            "api",
            "dotnet",  # deteccao inconclusiva sem PAT -> pergunta a linguagem
            "",  # versao .NET: enter mantem o default
            "",  # entrypoint .NET: enter usa o default
            "",  # applicationName: default
            "sim",  # usa APIM
            "",  # pathApiManagementService: default
            "api-payments",  # apiIds: valor unico vale para dev/qa/uat/prod (obrigatorio)
            "",  # host do ingress: default
            "nao",
            "",
        ]
    )

    result = _run_cli(tmp_path, inputs)
    assert result.returncode == 2
    assert "nao esta na allowlist" in result.stdout


def test_cli_sp_sem_user_token_falha_com_orientacao(tmp_path: Path) -> None:
    """Modo sp exige token do usuario (OBO); sem ele, sai com exit 2 antes de qualquer rede."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "AZDO_AUTH_MODE=sp\n"
        "AZDO_SP_TENANT_ID=tenant\n"
        "AZDO_SP_CLIENT_ID=client\n"
        "AZDO_SP_CLIENT_SECRET=secret\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n",
        encoding="utf-8",
    )
    result = _run_cli(tmp_path, "")
    assert result.returncode == 2
    assert "user-token" in result.stdout or "OBO" in result.stdout
