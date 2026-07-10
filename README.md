# devops-pipeline-agent

Agente conversacional self-service que cria pipelines CI/CD no Azure DevOps para deploy em AKS, com imagens no ACR e deploy via Helm.

## Status

POC em construcao. Veja `docs/architecture.md` (em breve) para a arquitetura completa.

## Como usar

```bash
pip install -e ".[dev]"
copy .env.example .env  # configure AZDO_*
devops-agent scaffold   # (ou `devops-agent chat`, alias equivalente)
```

Fluxo deterministico de perguntas fixas (repo, branch, namespace, tipo,
linguagem) - sem dependencia de LLM. O codigo deterministico decide *como*
gerar os artefatos via templates Jinja2. Util para CI/scripts.

O agente clona o repositorio da aplicacao, escreve os arquivos (Dockerfile,
azure-pipelines.yaml e o Helm em `helm/base` + `helm/environments`),
faz commit/push em branch nova e abre um unico PR.

## Estrutura

```
src/devops_agent/
  cli.py              Driver CLI (comandos scaffold e chat - mesmo fluxo)
  config.py           Settings (pydantic-settings)
  models/             Dominio (Pydantic) - SessionState, ToolResult, etc.
  orchestrator/       State machine + executor (clone/write/commit/push/PR)
  tools/              Tools deterministicas (validation, scaffold, git, azdo)
  templates/          Templates Jinja2 (pipeline, dockerfile, helm chart)
tests/                Testes (235: unit + integration + e2e CLI)
```

## Setup de desenvolvimento

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # Edite com seus valores
pytest
```
