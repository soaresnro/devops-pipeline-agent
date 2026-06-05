# devops-pipeline-agent

Agente conversacional self-service que cria pipelines CI/CD no Azure DevOps para deploy em AKS, com imagens no ACR e deploy via Helm.

## Status

POC em construcao. Veja `docs/architecture.md` (em breve) para a arquitetura completa.

## Como usar

Dois modos disponiveis:

### Modo conversacional (LLM)

```bash
pip install -e ".[llm,dev]"
copy .env.example .env  # configure AZURE_OPENAI_* e AZDO_*
devops-agent chat
```

Conversa natural com o agente. O LLM decide *quando* chamar cada tool;
o codigo deterministico decide *como* gerar os artefatos.

### Modo deterministico (sem LLM)

```bash
pip install -e ".[dev]"
devops-agent scaffold
```

Mesmo fluxo de 4 perguntas, mas via prompt fixo - sem dependencia de
Azure OpenAI. Util para CI/scripts.

Ambos os modos clonam os 2 repositorios (app + devops-templates),
escrevem arquivos, fazem commit/push em branches novas e abrem PRs.

## Estrutura

```
src/devops_agent/
  cli.py              Driver CLI (comandos scaffold e chat)
  config.py           Settings (pydantic-settings)
  models/             Dominio (Pydantic) - SessionState, ToolResult, etc.
  orchestrator/       State machine + executor (clone/write/commit/push/PR)
  tools/              Tools deterministicas (validation, scaffold, git, azdo)
  llm/                Agent loop + tool schemas + dispatcher + clients
  templates/          Templates Jinja2 (pipeline, dockerfile, helm chart)
tests/                Testes (101 hoje: unit + integration + e2e CLI)
```

## Setup de desenvolvimento

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # Edite com seus valores
pytest
```
