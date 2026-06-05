"""Smoke test do CLI conduzindo o fluxo conversacional via stdin.

Valida que as 4 perguntas, validacoes, dry_run e CONFIRM funcionam ponta-a-ponta.
Para evitar dependencia de Azure DevOps real, o teste responde 'nao' no CONFIRM,
encerrando a sessao antes do EXECUTE.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run_cli(env_file_dir: Path, stdin_text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "devops_agent.cli", "scaffold", "--requested-by", "tester@local"],
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
        "DEVOPS_TEMPLATES_TAG=v1\n"
        "DEVOPS_TEMPLATES_REVIEWERS=devops-team\n",
        encoding="utf-8",
    )

    inputs = "\n".join(
        [
            "https://dev.azure.com/contoso/Platform/_git/payments-api",
            "main",
            "payments-prod",
            "api",
            "dotnet",  # responde a pergunta de linguagem (deteccao inconclusiva sem PAT)
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
    assert "charts/payments-api/Chart.yaml" in stdout
    assert "sessao cancelada" in stdout.lower()


def test_cli_chat_sem_credenciais_falha_com_orientacao(tmp_path: Path) -> None:
    """chat exige Azure OpenAI - sem credenciais, instrui usar scaffold."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZDO_ORG=contoso\n"
        "DEVOPS_TEMPLATES_REPO_URL=https://dev.azure.com/contoso/Platform/_git/devops-templates\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "devops_agent.cli", "chat"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert "AZURE_OPENAI" in result.stdout
    assert "scaffold" in result.stdout


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
