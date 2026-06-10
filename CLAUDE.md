# CLAUDE.md

Guidance para o Claude Code trabalhar neste repositorio.

## Visao geral

Agente self-service (CLI **deterministica**, sem LLM) que automatiza criacao de pipelines CI/CD no Azure DevOps. Escopo MVP: deploy em AKS, imagens em ACR, Helm chart. Faz 4 perguntas (repo, branch, namespace, tipo: api/worker/web/cronjob) via state machine de perguntas fixas, detecta a linguagem do repo (dotnet/node/python/go) via Items API - perguntando ao usuario quando inconclusivo - e gera Dockerfile + azure-pipelines.yaml + Helm chart (`helm/manifests` + `helm/environments`) no repo da propria aplicacao, abrindo um unico PR.

Documento de arquitetura completo: [`docs/architecture.md`](docs/architecture.md).

Estado do projeto, backlog e preferencias de trabalho (PR-only, etc.): [`docs/agent-context.md`](docs/agent-context.md) — fonte de verdade portatil que viaja com o repo (a memoria local do Claude Code nao e clonada). Mantenha atualizado ao concluir features.

## Comandos comuns

**Setup:**
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env  # configurar AZDO_*
```

**Testes:**
```bash
.venv\Scripts\python -m pytest          # toda a suite (235 testes)
.venv\Scripts\python -m pytest tests/test_scaffold.py -v
.venv\Scripts\python -m pytest -k validation
```

**Rodar o agente:**
```bash
devops-agent scaffold    # fluxo deterministico (perguntas fixas, sem LLM)
devops-agent chat        # alias de scaffold (mesmo fluxo deterministico)
devops-agent version
```

## Estrutura do codigo

```
src/devops_agent/
  cli.py              Typer CLI (comandos scaffold e chat - mesmo fluxo deterministico)
  config.py           Settings via pydantic-settings (.env)
  models/             Pydantic: SessionState, Phase, AppType, ToolResult, ExecutionResult
  orchestrator/
    state_machine.py  Handlers puros das fases (sem LLM)
    executor.py       Pipeline EXECUTE: clone do repo da app + write + commit + push + PR
  tools/
    validation.py     Validacao de URL (allowlist), branch, namespace (RFC 1123), tipo, linguagem
    detection.py      Deteccao pura de linguagem por marcadores (csproj/package.json/...)
    scaffold.py       Render Jinja2 + dry_run + deteccao de colisao
    git_ops.py        subprocess git em workdir efemero
    azdo.py           Cliente httpx para Azure DevOps REST API (inclui list_root_items)
  templates/
    pipeline/         azure-pipelines.yaml.j2 por stack: dotnet/ (contrato real) e _default/ (generico)
    dockerfile/       dotnet/node/python/go Dockerfile.j2 (runtime-only, COPY artefatos da pipeline, Alpine/tz/health) + web (nginx)
    helm/basechart/   Chart.yaml.j2 (dependency na base chart) + microservice/worker values base (api/web/worker)
                      + _vendor/<base>/ (base charts descompactadas, copiadas verbatim do repo modelo)
    helm/_common/     Chart.yaml, values.yaml, templates (_helpers, serviceaccount) - so cronjob
    helm/cronjob/     cronjob.yaml (self-contained)
    environments/     microservice/ + worker/ (skeleton por ambiente) + values.yaml.j2 (cronjob)
tests/                235 testes: unit + integration (git contra remote local) + e2e CLI
```

## Decisoes arquiteturais nao-obvias

- **Geracao 100% deterministica (sem LLM).** Toda saida em arquivo vem de templates Jinja2 deterministicos, conduzidos pela state machine de perguntas fixas. A camada de LLM foi removida do produto (ver decisao abaixo). Nao reintroduza geracao de YAML/Dockerfile por modelo: a saida tem que ser reproduzivel byte-a-byte e auditavel.
- **azure-pipelines.yaml referencia template do DevOps por TAG**, nunca branch. Se mudar para branch, pipelines em producao vao quebrar quando alguem commitar no template.
- **Novos `extends.parameters` na pipeline sao gated por setting**, porque o template e pinado por tag e parametro nao declarado quebra a compilacao. Ex.: `language` so e emitido com `PIPELINE_PASS_LANGUAGE=true` (default false) — ligar apenas quando a tag do devops-templates ja aceitar o parametro. Mesmo padrao para futuros parametros.
- **O caminho do template de pipeline e escolhido pela linguagem** (`executor._pipeline_template_path`): `DEVOPS_TEMPLATE_<LINGUAGEM>` no .env, com fallback `DEVOPS_TEMPLATE_DEFAULT`. `web` reaproveita o template de node (build Node -> nginx). Selecao no executor; `dry_run`/`build_bundle` recebem `pipeline_template_path` (default `pipelines/k8s-app.yaml`).
- **O Helm vive no repo da PROPRIA aplicacao** (nao no repo do time de DevOps): chart em `helm/manifests/` e override por ambiente em `helm/environments/<env>/values.yaml` (ambientes via `DEVOPS_ENVIRONMENTS`, default dev/qa/uat/prod). Um unico clone + um unico PR; a pipeline le os manifests do checkout self (sem resource `manifests`). Os manifests NAO fixam `metadata.namespace` — o `helm -n <ns>-<env>` do template decide (senao da erro "namespace does not match").
- **Helm chart segue o padrao do time (AksHelmCharts):** api/web/worker geram uma chart FINA (`helm/basechart/Chart.yaml.j2`) que DEPENDE de uma base chart compartilhada (microservice-chart para api/web, worker-chart para worker), com values aninhados sob a chave da base chart (`microservice-chart:`/`worker-chart:`). NAO geramos templates (vem da base chart): a base chart e VENDORIZADA DESCOMPACTADA junto da chart fina — emitimos `helm/manifests/charts/<base>/` com os arquivos puros (Chart.yaml/values/templates), copiados verbatim do repo modelo (assets em `templates/helm/basechart/_vendor/<base>/`). Sem `.tgz` nem `Chart.lock`: o `helm` resolve a dependency declarada no Chart.yaml (sem `repository`) direto do diretorio `charts/<base>/`. Override por ambiente em `helm/environments/<env>/values.yaml` (skeleton: image por registry `acraks<env>eastus`, tag `__imageTag__`, ingresses/serviceAccount/workloadIdentity/env com placeholders). cronjob (sem base chart) mantem o chart self-contained (`helm/_common` + `helm/cronjob`). Selecao em `scaffold._BASE_CHARTS`.
- **O BLOCO de `extends.parameters` e por stack** (`scaffold._pipeline_stack` + `templates/pipeline/<stack>/azure-pipelines.yaml.j2`), porque cada template do time tem contrato proprio. Hoje so `dotnet` tem o contrato real - segue o modelo do time: `name`, `trigger` fixo (develop/release/hotfix/feature), `resources` (so o repo de templates, SEM ref; os manifests do Helm vem do proprio checkout self), `pool`, variable groups, e os params (deployAKS=true/deployAppService=false, ApplicationType, projectType, deploymentStrategy=rolling, image*). `imageName` usa `{{ app_name }}` (nome do repo em lowercase, ja que repositorio Docker exige lowercase e para ficar igual ao `image.repository` do Helm) e nao o macro `$(Build.Repository.Name)`, que preservaria maiusculas e quebraria a promocao entre ACRs. node/python/go/web usam `_default` (bloco generico) ate termos os reais. Para dotnet, no fluxo pergunta-se a versao (fase ASK_DOTNET_VERSION / tool set_dotnet_version; default 8.0.x) e o assembly de entrada (.dll) (fase ASK_DOTNET_ENTRYPOINT / tool set_dotnet_entrypoint; default {app_name}.dll); `pathApiManagementService`/`pathAutomation` sao derivados do app_name (sem prefixo `api-`).
- **Dockerfiles sao runtime-only** (modelo do time): sem build stage, `COPY . .` dos artefatos publicados pela pipeline; base Alpine, deps (tzdata/ca-certs/wget), timezone `America/Sao_Paulo`, HEALTHCHECK `/health` (api). dotnet: base aspnet (api) / runtime (worker/cronjob), tag derivada da versao; ENTRYPOINT = assembly informado. node/python/go com entrypoints placeholder (CI pre-builda).
- **PR-only, sempre.** Nunca implemente commit direto em main mesmo onde for permitido. ADR-007.
- **Allowlist rigida de org AzDO** (`AZDO_ORG` no .env). URLs de outras orgs sao recusadas em `validate_repo_url`. Nao remova essa verificacao.
- **Tools retornam `ToolResult`** (`{ok, data, error, error_code}`) — nunca propagam exception. Os handlers/validacoes capturam e convertem para esse contrato; quem chama inspeciona `ok` para decidir o proximo passo. Mantenha esse contrato.
- **State machine fica em `SessionState.phase`.** Os handlers puros em `orchestrator/state_machine.py` transicionam a fase; o CLI (`cli.py:_run_phase`) so roteia entrada/saida por fase. Sem agent loop.
- **Camada de LLM removida.** O agente e 100% deterministico (state machine + templates Jinja2). O modo `chat` virou alias do `scaffold`. Nao havia ganho real do LLM sobre o wizard de perguntas fixas, e o determinismo simplifica auditoria, custo e dependencias. Se um dia voltar conversacao, reintroduzir como um client novo sobre os mesmos handlers, sem deixar o modelo gerar artefatos.
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
2. UI: Teams bot ou Slack? (hoje so CLI; sem camada de LLM/conversacao)
3. Catalogo/API de namespaces oficiais do cluster?
4. Reviewers obrigatorios no `devops-templates`?
5. SLA tier-1 ou tier-2?

> Os pontos sobre Azure OpenAI (data residency, orcamento) cairam: a camada de LLM foi removida.

## Tarefas tipicas

- **Adicionar novo tipo de aplicacao** (ex.: `cronjob`): adicionar valor em `AppType` (`models/inputs.py`), criar pasta `templates/helm/<tipo>/templates/`, registrar defaults em `TYPE_DEFAULTS` (`tools/scaffold.py`), e adicionar o branch do `<tipo>` (EXPOSE/HEALTHCHECK/entrypoint) em cada `templates/dockerfile/<linguagem>.Dockerfile.j2`. Testes de scaffold ja parametrizam por `AppType` — vao cobrir automaticamente.
- **Adicionar nova linguagem**: adicionar valor em `Language` (`models/inputs.py`), marcadores em `_LANGUAGE_MARKERS` (`tools/detection.py`), aliases em `_LANGUAGE_ALIASES` (`tools/validation.py`), e `templates/dockerfile/<linguagem>.Dockerfile.j2` (condicional por `app_type`).
- **Adicionar nova validacao**: nova funcao em `tools/validation.py` retornando `ToolResult`. Se exigir uma nova pergunta no fluxo, adicionar a fase em `Phase` (`models/session.py`) e o handler em `orchestrator/state_machine.py`, alem de rotear em `cli.py:_run_phase`.
- **Adicionar nova pergunta ao fluxo**: novo valor em `Phase`, handler puro em `orchestrator/state_machine.py` (retorna o passo + transiciona a fase) e o branch correspondente em `cli.py:_run_phase`.
- **Mudar reviewer/branch de PR**: editar `orchestrator/executor.py`. Comportamento esta concentrado em `execute()`.

## Testes - guia rapido

- `conftest.py` — fixture autouse que desabilita a leitura do `.env` (tests hermeticos; o `.env` real do dev nao polui `Settings`).
- `test_validation.py` — regex/parsing puro. Rapido (<1s).
- `test_scaffold.py` — render de templates Jinja2 + parse YAML para validar estrutura. Rapido.
- `test_git_ops.py` — git real contra bare repo local em tmp_path. Ciclo completo clone→write→commit→push em ~10s.
- `test_azdo.py` — mocks HTTP via `respx`. Rapido.
- `test_cli_e2e.py` — subprocess do CLI (`scaffold` e `chat`) com stdin piped. ~3s.

Antes de marcar uma feature como concluida, rode `pytest` completo. NAO confie em "rodei alguns testes".

**Definition of Done:** alem de `pytest` verde e `mypy src` limpo, atualizar [`docs/agent-context.md`](docs/agent-context.md) **no mesmo PR** (estado/backlog, comandos ou preferencias). Detalhes la.
