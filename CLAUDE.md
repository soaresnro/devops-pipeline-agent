# CLAUDE.md

Guidance para o Claude Code trabalhar neste repositorio.

## Visao geral

Agente conversacional que automatiza criacao de pipelines CI/CD no Azure DevOps. Escopo MVP: deploy em AKS, imagens em ACR, Helm chart. Faz 4 perguntas (repo, branch, namespace, tipo: api/worker/web/cronjob), detecta a linguagem do repo (dotnet/node/python/go) via Items API - perguntando ao usuario quando inconclusivo - e gera Dockerfile + azure-pipelines.yaml no repo da aplicacao alem do Helm chart no repo `devops-templates` do time de DevOps, abrindo PRs nos dois.

Documento de arquitetura completo: [`docs/architecture.md`](docs/architecture.md).

Estado do projeto, backlog e preferencias de trabalho (PR-only, etc.): [`docs/agent-context.md`](docs/agent-context.md) — fonte de verdade portatil que viaja com o repo (a memoria local do Claude Code nao e clonada). Mantenha atualizado ao concluir features.

## Comandos comuns

**Setup:**
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[llm,dev]"
copy .env.example .env  # configurar AZDO_* e AZURE_OPENAI_*
```

**Testes:**
```bash
.venv\Scripts\python -m pytest          # toda a suite (262 testes)
.venv\Scripts\python -m pytest tests/test_scaffold.py -v
.venv\Scripts\python -m pytest -k validation
```

**Rodar o agente:**
```bash
devops-agent chat        # modo conversacional (requer Azure OpenAI)
devops-agent scaffold    # modo deterministico (perguntas fixas, sem LLM)
devops-agent version
```

## Estrutura do codigo

```
src/devops_agent/
  cli.py              Typer CLI (comandos chat e scaffold)
  config.py           Settings via pydantic-settings (.env)
  models/             Pydantic: SessionState, Phase, AppType, ToolResult, ExecutionResult
  orchestrator/
    state_machine.py  Handlers puros das fases (sem LLM)
    executor.py       Pipeline EXECUTE: clone duplo + write + commit + push + PR duplo
  tools/
    validation.py     Validacao de URL (allowlist), branch, namespace (RFC 1123), tipo, linguagem
    detection.py      Deteccao pura de linguagem por marcadores (csproj/package.json/...)
    scaffold.py       Render Jinja2 + dry_run + deteccao de colisao
    git_ops.py        subprocess git em workdir efemero
    azdo.py           Cliente httpx para Azure DevOps REST API (inclui list_root_items)
  llm/
    schemas.py        Tools em formato OpenAI function calling
    client.py         LLMClient (Protocol), AzureOpenAIClient, FakeLLMClient
    system_prompt.py  Prompt blindado contra injection
    dispatcher.py     Tool calls do LLM → handlers Python tipados
    agent.py          Agent loop conversacional com tool budget
  templates/
    pipeline/         azure-pipelines.yaml.j2 por stack: dotnet/ (contrato real) e _default/ (generico)
    dockerfile/       dotnet/node/python/go Dockerfile.j2 (runtime-only, COPY artefatos da pipeline, Alpine/tz/health) + web (nginx)
    helm/_common/     Chart.yaml, values.yaml, _helpers.tpl, serviceaccount
    helm/api/         deployment + service + ingress + hpa
    helm/worker/      deployment + keda-scaledobject
    helm/web/         deployment + service + ingress TLS + hpa
    environments/     values.yaml.j2 (override fino por ambiente)
tests/                262 testes: unit + integration (git contra remote local) + e2e CLI
```

## Decisoes arquiteturais nao-obvias

- **LLM nao gera artefatos.** Toda saida em arquivo vem de templates Jinja2 deterministicos. O LLM apenas decide *quando* chamar `propose_scaffold`/`execute_scaffold` com inputs ja validados. Nao adicione tools que pecam ao LLM para escrever YAML/Dockerfile.
- **azure-pipelines.yaml referencia template do DevOps por TAG**, nunca branch. Se mudar para branch, pipelines em producao vao quebrar quando alguem commitar no template.
- **Novos `extends.parameters` na pipeline sao gated por setting**, porque o template e pinado por tag e parametro nao declarado quebra a compilacao. Ex.: `language` so e emitido com `PIPELINE_PASS_LANGUAGE=true` (default false) — ligar apenas quando a tag do devops-templates ja aceitar o parametro. Mesmo padrao para futuros parametros.
- **O caminho do template de pipeline e escolhido pela linguagem** (`executor._pipeline_template_path`): `DEVOPS_TEMPLATE_<LINGUAGEM>` no .env, com fallback `DEVOPS_TEMPLATE_DEFAULT`. `web` reaproveita o template de node (build Node -> nginx). Selecao no executor; `dry_run`/`build_bundle` recebem `pipeline_template_path` (default `pipelines/k8s-app.yaml`).
- **Layout do Helm alinhado ao template do time:** chart em `helm/apps/<app>` e override por ambiente em `environments/<env>/<app>/values.yaml` (ambientes via `DEVOPS_ENVIRONMENTS`, default dev/qa/uat/prod). Os manifests NAO fixam `metadata.namespace` — o `helm -n <ns>-<env>` do template decide (senao da erro "namespace does not match").
- **O repo do Helm pode ser separado do repo de templates:** `DEVOPS_HELM_REPO_URL` (fallback para `DEVOPS_TEMPLATES_REPO_URL`) e `DEVOPS_HELM_REPO_BRANCH` (default `main`, ex.: `master`) — clone + PR do chart vao para esse repo/branch (`executor._parse_helm_repo`). O repo de templates so e referenciado por tag na pipeline.
- **O BLOCO de `extends.parameters` e por stack** (`scaffold._pipeline_stack` + `templates/pipeline/<stack>/azure-pipelines.yaml.j2`), porque cada template do time tem contrato proprio. Hoje so `dotnet` tem o contrato real - segue o modelo do time: `name`, `trigger` fixo (develop/release/hotfix/feature), `resources` (templates SEM ref + manifests = repo do Helm por branch), `pool`, variable groups, e os params (deployAKS=true/deployAppService=false, ApplicationType, projectType, deploymentStrategy=rolling, image*). node/python/go/web usam `_default` (bloco generico) ate termos os reais. Para dotnet, no fluxo pergunta-se a versao (fase ASK_DOTNET_VERSION / tool set_dotnet_version; default 8.0.x) e o assembly de entrada (.dll) (fase ASK_DOTNET_ENTRYPOINT / tool set_dotnet_entrypoint; default {app_name}.dll); `pathApiManagementService`/`pathAutomation` sao derivados do app_name (sem prefixo `api-`).
- **Dockerfiles sao runtime-only** (modelo do time): sem build stage, `COPY . .` dos artefatos publicados pela pipeline; base Alpine, deps (tzdata/ca-certs/wget), timezone `America/Sao_Paulo`, HEALTHCHECK `/health` (api). dotnet: base aspnet (api) / runtime (worker/cronjob), tag derivada da versao; ENTRYPOINT = assembly informado. node/python/go com entrypoints placeholder (CI pre-builda).
- **PR-only, sempre.** Nunca implemente commit direto em main mesmo onde for permitido. ADR-007.
- **Allowlist rigida de org AzDO** (`AZDO_ORG` no .env). URLs de outras orgs sao recusadas em `validate_repo_url`. Nao remova essa verificacao.
- **Tool calls retornam dict** `{ok, data, error, error_code}` — nunca exception ao LLM. Se um handler lancar excecao, o dispatcher captura e converte. Mantenha esse contrato.
- **Inputs do usuario sao envoltos em `<user_input>...</user_input>`** antes de ir ao LLM (mitiga prompt injection). Ver `llm/system_prompt.py:wrap_user_input`.
- **State machine fica em `SessionState.phase`**, nao no agent loop. O dispatcher transiciona; o agent loop so roteia mensagens.
- **LangGraph nao foi adotado.** Agent loop puro com OpenAI tool calling cobre o caso. Revisitar so se precisar de paralelismo ou persistencia multi-turn.
- **Dois modos de auth** (`AZDO_AUTH_MODE`): `pat` (POC/dev) e `sp` (producao, ADR-001). No modo `sp`, `tools/auth.py` adquire o app token do Service Principal (ClientSecretCredential ou DefaultAzureCredential/Managed Identity) e, ANTES de qualquer clone, `executor._check_user_permission` valida `Contribute` do usuario via OBO (fail-closed: recusa sem usar o SP). O token do usuario (`user_assertion`) vem de `--user-token`/`AZDO_AGENT_USER_ASSERTION` ou, se `AZDO_LOGIN_CLIENT_ID` estiver setado, de login interativo device code (`auth.acquire_user_assertion_interactive`, disparado automaticamente no modo sp sem token). Git over HTTPS usa header Bearer no modo sp (ver `GitAuth.bearer`).

## Convencoes

- **pt-br** em mensagens ao usuario, comentarios, commits e documentacao.
- **Sem emojis** em codigo, mensagens ou commits (a menos que o usuario peca).
- **Comentarios so quando WHY nao e obvio**. Nao explique WHAT.
- Tipagem strict: `from __future__ import annotations`, anotacoes em tudo, `mypy` config strict.
- Tools sempre tipam args com Pydantic antes de processar.

## Pontos em aberto (a fechar conforme desenvolvimento avanca)

Ver `docs/architecture.md` secao 12. Resumo:

1. Templates DevOps em mono-repo versionado ou multiplos?
2. UI: Teams bot ou Slack?
3. Catalogo/API de namespaces oficiais do cluster?
4. Reviewers obrigatorios no `devops-templates`?
5. Data residency Azure OpenAI?
6. SLA tier-1 ou tier-2?
7. Orcamento Azure OpenAI?

## Tarefas tipicas

- **Adicionar novo tipo de aplicacao** (ex.: `cronjob`): adicionar valor em `AppType` (`models/inputs.py`), criar pasta `templates/helm/<tipo>/templates/`, registrar defaults em `TYPE_DEFAULTS` (`tools/scaffold.py`), e adicionar o branch do `<tipo>` (EXPOSE/HEALTHCHECK/entrypoint) em cada `templates/dockerfile/<linguagem>.Dockerfile.j2`. Testes de scaffold ja parametrizam por `AppType` — vao cobrir automaticamente.
- **Adicionar nova linguagem**: adicionar valor em `Language` (`models/inputs.py`), marcadores em `_LANGUAGE_MARKERS` (`tools/detection.py`), aliases em `_LANGUAGE_ALIASES` (`tools/validation.py`), e `templates/dockerfile/<linguagem>.Dockerfile.j2` (condicional por `app_type`).
- **Adicionar nova validacao**: nova funcao em `tools/validation.py` retornando `ToolResult`. Se for tool exposta ao LLM, adicionar schema em `llm/schemas.py` e handler em `llm/dispatcher.py`.
- **Mudar reviewer/branch de PR**: editar `orchestrator/executor.py`. Comportamento esta concentrado em `execute()`.
- **Trocar provider de LLM**: implementar `LLMClient` protocol (`llm/client.py`); resto do codigo nao muda.

## Testes - guia rapido

- `conftest.py` — fixture autouse que desabilita a leitura do `.env` (tests hermeticos; o `.env` real do dev nao polui `Settings`).
- `test_validation.py` — regex/parsing puro. Rapido (<1s).
- `test_scaffold.py` — render de templates Jinja2 + parse YAML para validar estrutura. Rapido.
- `test_git_ops.py` — git real contra bare repo local em tmp_path. Ciclo completo clone→write→commit→push em ~10s.
- `test_azdo.py` — mocks HTTP via `respx`. Rapido.
- `test_llm_dispatcher.py` — cada handler do dispatcher.
- `test_llm_agent.py` — agent loop com `FakeLLMClient` que replays respostas scriptadas.
- `test_cli_e2e.py` — subprocess do CLI com stdin piped. ~3s.

Antes de marcar uma feature como concluida, rode `pytest` completo. NAO confie em "rodei alguns testes".

**Definition of Done:** alem de `pytest` verde e `mypy src` limpo, atualizar [`docs/agent-context.md`](docs/agent-context.md) **no mesmo PR** (estado/backlog, comandos ou preferencias). Detalhes la.
