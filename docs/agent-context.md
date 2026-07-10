# Contexto do agente (estado + preferencias de trabalho)

Doc **versionado** para que qualquer sessao do Claude Code — em qualquer maquina —
retome o contexto essencial apenas clonando o repo. A memoria local do Claude Code
mora em `~/.claude/` (fora do repo) e **nao** e clonada; este arquivo e a fonte de
verdade portatil. Mantenha-o atualizado ao concluir/abrir features.

## Definition of Done

Uma feature/mudanca so esta "pronta" quando, **no mesmo PR**:
1. Codigo implementado seguindo as convencoes do `CLAUDE.md`.
2. `pytest` completo verde e `mypy src` limpo.
3. **`docs/agent-context.md` atualizado** (tabela de PRs/estado, comandos ou
   preferencias) refletindo a mudanca — este doc e manual e so fica correto se
   atualizado por commit.

## Preferencias de trabalho (seguir sempre)

- **PR-only.** Toda mudanca vai para um branch + commit + push do branch + PR.
  **Nunca** push direto na `main`, nem deletar branches remotos, sem o usuario pedir.
  Commit local e push de branch de feature passam normalmente. Coerente com o ADR-007.
- **gh CLI** esta disponivel e autenticado (conta `soaresnro`); usar `gh pr create`
  direto, sem fallback de API REST. Em maquina nova: refazer `gh auth login`.
- **Idioma:** pt-br em mensagens, comentarios, commits e PRs. Sem emojis em codigo/commits.
- **Identidade git:** repo pessoal (path `pessoal/`); o `user.email` **local** e
  `soaresnro@gmail.com` (a config global usa o email de trabalho). Manter o email
  pessoal nos repos pessoais.
- Antes de marcar uma feature como concluida: rodar `pytest` completo e `mypy src`.

## Estado do projeto (atualizado em 2026-07-10)

**Alinhamento 1:1 ao repo modelo `epharma-api-utils` (2026-07-10).** Os artefatos gerados passaram
a espelhar exatamente o repo real do time (`epharma-api-utils`, branch `configuracao/aks`). Mudancas:
(1) **Helm** em `helm/base/` (era `helm/manifests/`), com a base chart VENDORIZADA EMPACOTADA
(`helm/base/charts/<base>-<versao>.tgz` + `helm/base/Chart.lock`; reverte o formato descompactado do
PR #24) e dependency no Chart.yaml com `repository: file://.../AksHelmBaseCharts/...`. O `.tgz` volta a
ser binario (`ScaffoldedFile.binary_b64` + `write_bytes` no git_ops), recuperado do historico git
(commit pai de `d0d8a5d`). Validado com `helm lint`/`helm template` reais. (2) **Pipeline dotnet**
espelha o `template-aks.yml`: `enableApim: true` (so api; substitui deployAKS/deployAppService),
`imageName: $(Build.Repository.Name)` (macro; reverte o lowercase do PR #22), e `resources.templates`
com `ref: refs/heads/{DEVOPS_TEMPLATES_BRANCH}` (branch `master`, novo setting). (3) **Skeleton de
environments** (microservice) com DOIS ingresses (primary `private-nginx` + legacy `private-legacy-nginx`,
hosts/secrets derivados por ambiente na convencao epharma); o skeleton e gerado para TODOS os
ambientes, incluindo `dev` (a referencia deixa dev vazio, mas o agente gera o override — ajuste
em fix/gerar-dev-values, 2026-07-10). (4) **Fluxo** ganhou 3
perguntas para dotnet api: `applicationName` (ASK_APPLICATION_NAME), `pathApiManagementService`
(ASK_APIM_PATH) e host base do ingress (ASK_INGRESS_HOST), todas com defaults derivados; web pergunta
so o host; worker/cronjob pulam APIM/ingress. (5) **Dockerfile dotnet** ganhou `icu-libs` +
`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=false`. **247 testes** verdes, `mypy --strict` limpo. Setting novo:
`DEVOPS_TEMPLATES_BRANCH` (default `master`); recomenda-se `DEVOPS_TEMPLATE_DOTNET=pipelines/template-aks.yml`.

**Helm no repo da propria aplicacao (2026-06-10).** O agente passou a gerar TUDO no repositorio
da propria app, com um unico clone e um unico PR: Dockerfile, azure-pipelines.yaml, chart em
`helm/manifests/` e overrides por ambiente em `helm/environments/<env>/values.yaml`. Nao ha mais
PR no repo do time de DevOps; o resource `manifests` saiu da pipeline dotnet (os manifests vem do
checkout self) e o `helmChartPath` do template `_default` aponta para `helm/manifests`. A base
chart e vendorizada **descompactada** em `helm/manifests/charts/<base>/` (arquivos puros, sem
`.tgz` nem `Chart.lock`; a dependency no Chart.yaml nao tem `repository` e o helm resolve do
diretorio — validado com `helm lint`/`helm template` reais). Com isso cairam: o suporte a binario
em `ScaffoldedFile` (`binary_b64`), os settings `DEVOPS_HELM_REPO_URL`/`DEVOPS_HELM_REPO_BRANCH`/
`DEVOPS_TEMPLATES_REVIEWERS`, o `HelmRepo`/`_parse_helm_repo` do executor e os campos
`helm_branch`/`helm_pr_url` de `ExecutionResult`. `ScaffoldBundle` virou lista unica (`files`).

**Camada de LLM removida (2026-06-09).** O agente e 100% deterministico: state machine de
perguntas fixas + templates Jinja2. O pacote `src/devops_agent/llm/` (agent loop, dispatcher,
schemas, system_prompt, clients Azure OpenAI), o extra `[llm]` (openai/langgraph), os settings
`AZURE_OPENAI_*` e os testes `test_llm_*` foram apagados. O comando `chat` virou **alias** do
`scaffold` (mesmo fluxo deterministico) por compatibilidade. Motivo: o LLM nao trazia ganho real
sobre o wizard de perguntas fixas e o determinismo simplifica auditoria, custo e dependencias.

MVP/POC completo e verde: **235 testes**, `mypy --strict` limpo. O Helm segue o padrao do time
(AksHelmCharts): api/web/worker geram uma chart FINA que depende de uma base chart compartilhada
(microservice-chart / worker-chart), com values aninhados sob a chave da base chart e override por
ambiente skeleton (image `acraks<env>eastus`, tag `__imageTag__`, ingresses/serviceAccount/
workloadIdentity/env placeholders). cronjob continua self-contained. Os assets da base chart ficam
em `templates/helm/basechart/_vendor/<base>/`. O wheel empacota tudo via
`packages = ["src/devops_agent"]`.
Os Dockerfiles sao runtime-only
(modelo do time): sem build stage, COPY dos artefatos publicados pela pipeline, Alpine + tzdata/ca-certs
+ timezone America/Sao_Paulo + HEALTHCHECK /health (api). Para dotnet, o fluxo pergunta versao E
assembly de entrada (.dll, default {app_name}.dll). O template de pipeline `dotnet` segue
o modelo real do time (name, trigger develop/release/hotfix/feature, resources templates+manifests por
branch, pool, variable groups, params com deploymentStrategy/image*; `path*` derivado do app_name sem
prefixo `api-`). `imageName` usa `{{ app_name }}` (repo em lowercase) — nao o macro `$(Build.Repository.Name)`,
que preservava maiusculas e quebrava a promocao entre ACRs ("repository name must be lowercase").
Testes sao hermeticos (`tests/conftest.py` desabilita a leitura do `.env`).
`validate_repo_url` aceita
os formatos `dev.azure.com/<org>/...` e o legado `<org>.visualstudio.com/<projeto>/_git/<repo>`
(org = subdominio). A allowlist `AZDO_ORG` compara com a org extraida da URL.

**Adaptacao a templates reais do time (concluida para .NET):** (PR1) o bloco `extends.parameters` da
pipeline e por stack (`templates/pipeline/<stack>/azure-pipelines.yaml.j2`); `dotnet` usa o contrato
real (applicationName/ApplicationType/dotNetVersion/deployAKS=true/deployAppService=false; versao
perguntada no fluxo, default 8.0.x); node/python/go/web seguem no `_default` generico ate recebermos
os templates reais. (PR2) o Helm passou a gerar override por ambiente (ambientes via
`DEVOPS_ENVIRONMENTS`); os manifests nao fixam `metadata.namespace` (o `helm -n <ns>-<env>` do
template decide). Desde 2026-06-10 o layout e `helm/manifests` + `helm/environments/<env>/values.yaml`
no repo da propria app. Falta, conforme os templates
reais de node/python/go chegarem, criar os respectivos `templates/pipeline/<stack>/`. Publicado no GitHub
em `https://github.com/soaresnro/devops-pipeline-agent` (licenca MIT).

Backlog escolhido **100% concluido** — cada item virou um PR:

| PR | Item |
|----|------|
| #2 | Tipo de app `cronjob` (Helm `kind: CronJob`) |
| #3 | Deteccao automatica de linguagem (Items API, Dockerfile por linguagem) |
| #4 | Auth Service Principal + WIF + OBO (`AZDO_AUTH_MODE=sp`, fail-closed) |
| #5 | `schedule` do cronjob virou pergunta (fase ASK_SCHEDULE / tool set_schedule) |
| #6 | Passa `language` na pipeline, gated por `PIPELINE_PASS_LANGUAGE` (default off) |
| #7 | `detect_language_step` usa o app token do SP no modo `sp` |
| #8 | Login MSAL interativo device code (`AZDO_LOGIN_CLIENT_ID`) |
| #9 | OBO sem client secret (client assertion via Managed Identity) |

**Pendente para producao** (infra pura, nao iniciado): hosting em Azure Container
Apps, Teams bot / SPA front-end, App Insights / OpenTelemetry, Redis para sessao
persistente.

## Subir o projeto numa maquina nova

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"      # nao ha mais extra de LLM
copy .env.example .env       # preencher AZDO_*
gh auth login                # refazer login do gh
pytest                       # deve ficar verde
```

`.env` e `.venv` sao locais (gitignored / nao versionados) e precisam ser recriados.

## Testar o fluxo contra um Azure DevOps real (resumo)

Use `AZDO_AUTH_MODE=pat` (o modo `sp` exige app registrations + OBO; e producao).
PAT com escopo **Code (Read & Write)**.

1. `devops-agent scaffold` faz as perguntas, mostra o resumo e pergunta `confirma?`.
   Responder `nao` = dry-run seguro (nada e escrito). `sim` = abre UM PR no repo da
   app (Dockerfile + azure-pipelines.yaml + helm/base + helm/environments).
2. Para a pipeline **compilar/rodar**, o template referenciado precisa existir no
   repo `devops-templates` **na tag** `DEVOPS_TEMPLATES_TAG` (referencia por tag,
   nunca branch). O caminho e escolhido pela linguagem via `DEVOPS_TEMPLATE_<LINGUAGEM>`
   (fallback `DEVOPS_TEMPLATE_DEFAULT=pipelines/k8s-app.yaml`; web usa o de node). Um
   stub que so ecoa os parametros ja roda verde sem AKS/ACR.
3. Criar a *definicao* da Pipeline no Azure DevOps apontando para `azure-pipelines.yaml`
   (o agente entrega os arquivos via PR, mas nao cria a Pipeline nem o cluster).
