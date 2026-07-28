# CLAUDE.md

Guidance para o Claude Code trabalhar neste repositorio.

## Visao geral

Agente self-service (CLI **deterministica**, sem LLM) que automatiza criacao de pipelines CI/CD no Azure DevOps. Escopo MVP: deploy em AKS, imagens em ACR, Helm chart. Faz 4 perguntas (repo, branch, namespace, tipo: api/worker/web/cronjob) via state machine de perguntas fixas, detecta a linguagem do repo (dotnet/node/python/go) via Items API - perguntando ao usuario quando inconclusivo - e gera Dockerfile + azure-pipelines.yaml + Helm chart (`helm/base` + `helm/environments`) no repo da propria aplicacao, abrindo um unico PR. O layout dos artefatos espelha 1:1 o repo modelo `epharma-api-utils` (branch `configuracao/aks`).

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
.venv\Scripts\python -m pytest          # toda a suite (255 testes)
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
    helm/basechart/   Chart.yaml.j2 (dependency na base chart, com repository file://) + microservice/worker values base (api/web/worker)
                      + _vendor/<base>-<versao>.tgz + .lock (base charts EMPACOTADAS + Chart.lock, saidas reais de helm dependency build)
    helm/_common/     Chart.yaml, values.yaml, templates (_helpers, serviceaccount) - so cronjob
    helm/cronjob/     cronjob.yaml (self-contained)
    environments/     microservice/ (dois ingresses) + worker/ (skeleton por ambiente) + values.yaml.j2 (cronjob)
tests/                255 testes: unit + integration (git contra remote local) + e2e CLI
```

## Decisoes arquiteturais nao-obvias

- **Geracao 100% deterministica (sem LLM).** Toda saida em arquivo vem de templates Jinja2 deterministicos, conduzidos pela state machine de perguntas fixas. A camada de LLM foi removida do produto (ver decisao abaixo). Nao reintroduza geracao de YAML/Dockerfile por modelo: a saida tem que ser reproduzivel byte-a-byte e auditavel.
- **Referencia ao repo de templates do DevOps difere por stack.** O stack `_default` (node/python/go/web) referencia por TAG (`DEVOPS_TEMPLATES_TAG`), pinando a versao — a regra de seguranca original (commit no template nao afeta pipelines pinadas). O stack `dotnet` espelha o contrato real do time (`template-aks.yml`), que referencia por BRANCH (`refs/heads/{DEVOPS_TEMPLATES_BRANCH}`, default `master`). Ao evoluir o stack dotnet, ciente de que qualquer commit no template afeta todas as pipelines dotnet imediatamente (decisao deliberada para alinhar 1:1 com `epharma-api-utils`).
- **Novos `extends.parameters` na pipeline sao gated por setting**, porque o template e pinado por tag e parametro nao declarado quebra a compilacao. Ex.: `language` so e emitido com `PIPELINE_PASS_LANGUAGE=true` (default false) — ligar apenas quando a tag do devops-templates ja aceitar o parametro. Mesmo padrao para futuros parametros.
- **O caminho do template de pipeline e escolhido pela linguagem** (`executor._pipeline_template_path`): `DEVOPS_TEMPLATE_<LINGUAGEM>` no .env, com fallback `DEVOPS_TEMPLATE_DEFAULT`. `web` reaproveita o template de node (build Node -> nginx). Selecao no executor; `dry_run`/`build_bundle` recebem `pipeline_template_path` (default `pipelines/k8s-app.yaml`).
- **O Helm vive no repo da PROPRIA aplicacao** (nao no repo do time de DevOps): chart em `helm/base/` e override por ambiente em `helm/environments/<env>/values.yaml` (ambientes via `DEVOPS_ENVIRONMENTS`, default dev/qa/uat/prod). Um unico clone + um unico PR; a pipeline le os manifests do checkout self (sem resource `manifests`). Os manifests NAO fixam `metadata.namespace` — o `helm -n <ns>-<env>` do template decide (senao da erro "namespace does not match"). Layout e estrutura espelham 1:1 o repo modelo `epharma-api-utils` (branch `configuracao/aks`).
- **Helm chart segue o padrao do time (AksHelmBaseCharts):** api/web/worker geram uma chart FINA (`helm/basechart/Chart.yaml.j2`) que DEPENDE de uma base chart compartilhada (microservice-chart para api/web, worker-chart para worker), com values aninhados sob a chave da base chart (`microservice-chart:`/`worker-chart:`). NAO geramos templates (vem da base chart): a base chart e VENDORIZADA EMPACOTADA — emitimos `helm/base/charts/<base>-<versao>.tgz` + `helm/base/Chart.lock` (par consistente, saidas reais de `helm dependency build`, recuperadas do repo modelo; assets em `templates/helm/basechart/_vendor/<base>-<versao>.tgz`/`.lock`). O Chart.yaml declara a dependency com `repository: file://../../../../AksHelmBaseCharts/base-charts/<base>` (metadado; o `helm` renderiza pelo `.tgz` em `charts/`, validado com `helm lint`/`helm template`). O `.tgz` e binario: `ScaffoldedFile.binary_b64` carrega os bytes e `git_ops.write_files` faz `write_bytes`. Override por ambiente em `helm/environments/<env>/values.yaml` (microservice: DOIS ingresses primary `private-nginx` + legacy `private-legacy-nginx`, hosts/secrets derivados por ambiente na convencao epharma; TODOS os ambientes incl. `dev` recebem o skeleton — a referencia deixa dev vazio, mas o agente gera o override para o usuario ajustar; image por registry `acraks<env>eastus`, tag `__imageTag__`, serviceAccount `sa-{namespace}-{applicationName}`, workloadIdentity com placeholder, e `env` com defaults: `ASPNETCORE_ENVIRONMENT` por ambiente (dev/qa=Development, uat=Staging, prod=Production; helper `scaffold.aspnetcore_environment`, fallback Development), `WEBSITE_TIME_ZONE=America/Sao_Paulo`, e `SwaggerEnabled=true` so para api). cronjob (sem base chart) mantem o chart self-contained (`helm/_common` + `helm/cronjob`). Selecao em `scaffold._BASE_CHARTS`.
- **O BLOCO de `extends.parameters` e por stack** (`scaffold._pipeline_stack` + `templates/pipeline/<stack>/azure-pipelines.yaml.j2`), porque cada template do time tem contrato proprio. Hoje so `dotnet` tem o contrato real - espelha 1:1 o `template-aks.yml` de `epharma-api-utils`: `name`, `trigger` fixo (develop/release/hotfix/feature), `resources` (repo de templates com `ref: refs/heads/{DEVOPS_TEMPLATES_BRANCH}`; os manifests do Helm vem do proprio checkout self), `pool`, variable groups, e os params (`enableApim` true so para api, ApplicationType, projectType=webapi, deploymentStrategy=rolling, dotNetVersion, `apiId` so para api, image*). `imageName` usa o macro `$(Build.Repository.Name)` (fiel ao modelo do time; funciona porque os repos sao lowercase). node/python/go/web usam `_default` (bloco generico, `helmChartPath: helm/base`) ate termos os reais. Para dotnet api, o fluxo pergunta: versao (ASK_DOTNET_VERSION; default 8.0.x), assembly (.dll) (ASK_DOTNET_ENTRYPOINT; default {app_name}.dll), `applicationName` (ASK_APPLICATION_NAME; default = nome do repo; se comecar com `{namespace}-` o prefixo e removido via `scaffold.strip_namespace_prefix`, para nao duplicar o namespace no SA `sa-{namespace}-{applicationName}`), `pathApiManagementService` (ASK_APIM_PATH; default = applicationName sem prefixo `api-`), `apiId` do APIM (ASK_API_ID; OBRIGATORIO, sem default) e host base do ingress (ASK_INGRESS_HOST; default = `services<path>`). worker/cronjob dotnet pulam APIM/ingress (nao passam apiId); web pergunta so o host. `DEVOPS_TEMPLATE_DOTNET=pipelines/template-aks.yml`.
- **Dockerfiles sao runtime-only** (modelo do time): sem build stage, `COPY . .` dos artefatos publicados pela pipeline; base Alpine, deps (tzdata/ca-certs/wget/icu-libs), timezone `America/Sao_Paulo`, HEALTHCHECK `/health` (api). dotnet: base aspnet (api) / runtime (worker/cronjob), tag derivada da versao; ENTRYPOINT = assembly informado. node/python/go com entrypoints placeholder (CI pre-builda).
- **Unified service tagging do Datadog so em uat/prod.** `helm/environments/<env>/values.yaml` recebe `podLabels` (`tags.datadoghq.com/env|service|version`) e as envs `DD_ENV`/`DD_SERVICE`/`DD_VERSION` apenas nos ambientes de `scaffold._DATADOG_ENVIRONMENTS` (uat/prod — os que tem agente coletando); dev/qa nao recebem nada. Nome do servico: `{namespace}-{applicationName}-k8s-{env}` (`scaffold.datadog_service`; o applicationName ja vem sem o prefixo do namespace, entao nao duplica). **ATENCAO:** microservice-chart 0.5.0 e worker-chart 1.0.0 NAO leem `.Values.podLabels` — o pod template so fixa `app` e `azure.workload.identity/use`. As envs `DD_*` funcionam, mas as `podLabels` de api/web/worker ficam inertes ate a base chart passar a renderiza-las (o chart do cronjob e nosso e ja renderiza). Ao subir a base chart com suporte, nada muda no agente alem de re-vendorizar.
- **O bloco `env` do override e montado em Python, nao no template.** `scaffold.application_env` reune, nesta ordem: defaults por ambiente (ASPNETCORE_ENVIRONMENT/WEBSITE_TIME_ZONE/SwaggerEnabled), `dotnet_runtime_env` e as `DD_*`. Os templates de `environments/` so iteram o dict (`app_env`), porque o mesmo conteudo precisa sair em tres formatos: map sob `microservice-chart:`, map sob `worker-chart:` e lista de `{name, value}` no chart self-contained do cronjob. Adicione variaveis novas no helper, nao nos `.j2`.
- **Variaveis de runtime vivem no values de cada ambiente, nao no Dockerfile.** O Dockerfile dotnet mantem UMA unica `ENV`: `DOTNET_RUNNING_IN_CONTAINER=true` (descreve a imagem, nao o ambiente). `TZ`, `ASPNETCORE_URLS` (so api), `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT` e os `Logging__LogLevel__*` sao emitidos no `env` de `helm/environments/<env>/values.yaml` por `scaffold.dotnet_runtime_env`, para serem ajustaveis por ambiente sem rebuild da imagem. A imagem ainda instala `tzdata` e faz o symlink de `/etc/localtime` (default), mas sem `ENV TZ`. `web` fica de fora (SPA estatica em nginx) e node/python/go ainda fixam as suas ENVs no Dockerfile. Ao adicionar uma ENV de runtime nova, coloque-a no values, nao no Dockerfile.
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
