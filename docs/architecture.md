# Arquitetura — devops-pipeline-agent

**Versao:** 1.0 (MVP)
**Data:** 2026-05-29

---

## Sumario executivo

Agente conversacional **single-turn-by-turn** que automatiza a criacao de pipelines CI/CD no Azure DevOps com deploy em AKS, imagens no ACR e Helm. Hospedado em **Azure Container Apps**, escrito em **Python**, usando **Azure OpenAI (GPT-4.1)** como LLM com **tool use** explicito. O agente atua como **scaffolder deterministico assistido por LLM** — o LLM conduz a conversa e extrai/valida inputs, mas a geracao dos artefatos (Dockerfile, `azure-pipelines.yaml`, Helm chart) e feita por **templates Jinja2 versionados**, nao por geracao livre do LLM. Toda alteracao vai por **Pull Request** (nunca commit direto em branch protegida). Autenticacao contra Azure DevOps via **Service Principal dedicado** combinado com **verificacao OBO** da permissao do usuario humano.

**Regra de ouro:** o LLM conversa e decide *o que* fazer; codigo deterministico decide *como* fazer.

> **Nota de implementacao (atualizada):** a camada de LLM foi **removida** do produto. O
> agente roda **somente em modo deterministico** (perguntas fixas via state machine); os
> comandos `scaffold` e `chat` rodam o mesmo fluxo (`chat` e alias). As secoes 1-12 abaixo
> descrevem a visao original (LLM como condutor, Teams bot, Azure OpenAI) que **nao** esta
> implementada. A secao 13 e o estado real. Fonte de verdade do estado: [`agent-context.md`](agent-context.md).

---

## 1. Visao geral

```
        USUARIO (Dev - Teams / SPA / CLI)
                │ HTTPS + JWT (Entra ID)
                ▼
   ┌────────────────────────────────────────────┐
   │      BACKEND (Azure Container Apps)        │
   │                                            │
   │   FastAPI (/chat SSE)                      │
   │        │                                   │
   │        ▼                                   │
   │   Agent Orchestrator                       │
   │   (state machine + tool use)               │
   │        │ tool calls                        │
   │        ▼                                   │
   │   Tool Executor (Python deterministico)    │
   │   ┌──────────────┐ ┌──────────────────┐    │
   │   │ Validation   │ │ Scaffold (Jinja) │    │
   │   │ Git          │ │ AzDO (REST)      │    │
   │   └──────────────┘ └──────────────────┘    │
   └────────────────┬───────────────────────────┘
                    ▼
   Azure OpenAI · Azure DevOps · Key Vault ·
   Redis (sessao) · App Insights · DevOps Templates Repo
```

### Principios arquiteturais

1. LLM como condutor de conversa, nao como gerador de artefatos
2. Tool use e a fronteira de seguranca
3. Stateless por sessao, idempotente por operacao
4. PR-first (nunca push direto)
5. Templates do DevOps sao source of truth

---

## 2. Stack tecnologica

| Camada | Escolha | Por que |
|---|---|---|
| **LLM** | Azure OpenAI `gpt-4.1` (ou `4o`) | Private endpoint, residencia de dados, billing integrado. Nao usar `mini` — erra tool use sutilmente |
| **Linguagem** | Python 3.12+ | Ecossistema LLM/tool-use e Python-first |
| **Web** | FastAPI + SSE | SSE mais simples que WebSocket para stream de tokens |
| **Templating** | Jinja2 | Padrao Python |
| **Hosting** | Azure Container Apps | Scale-to-zero, MI, ingress pronto; AKS e overkill |
| **Estado sessao** | Azure Cache for Redis | TTL 1h |
| **Secrets** | Key Vault + Managed Identity | Sem senha no app |
| **Observabilidade** | App Insights + Log Analytics | Ja esta no ecossistema |
| **Git client** | `subprocess git` | Mais previsivel que libgit2 |
| **AzDO client** | `httpx` direto | SDK Microsoft e pesado/incompleto para ~10 endpoints |

**Nota MVP:** LangGraph **nao** foi adotado. Para o fluxo de 4 perguntas + execucao, um agent loop puro com OpenAI tool calling cumpre o ADR-004 (state machine explicita) sem o peso adicional. Revisitar caso surja necessidade de paralelismo de tools ou persistencia multi-turn.

---

## 3. Componentes principais

### 3.1 Chat/UI — Teams Bot
Recomendacao: comecar com **Teams bot** (Bot Framework + Azure Bot Service). Empresa Azure-heavy = M365. Identity propagation gratis. Fallback: SPA React minima com MSAL.js.

### 3.2 Orquestrador — Maquina de estados
```
GREET → ASK_REPO → ASK_BRANCH → ASK_NS → ASK_TYPE
      → VALIDATE → CONFIRM → EXECUTE → DONE | ABORTED | ERROR
```

### 3.3 Catalogo de tools (formato OpenAI function calling)

| Tool | Funcao |
|---|---|
| `validate_repo_url` | Regex + normalizacao + allowlist de org |
| `validate_branch` | Regras git basicas |
| `validate_namespace` | RFC 1123 + reservados |
| `validate_app_type` | Enum api/worker/web |
| `propose_scaffold` | Renderiza tudo em memoria, detecta colisoes |
| `execute_scaffold` | Clone do repo da app + write + commit + push + PR |
| `abort_session` | Cancela sessao + audit |

Toda tool retorna `{ok, data, error, error_code}` — nunca exception para o LLM. O LLM ve **verbos do dominio**, nao shell. Argumentos sao validados via Pydantic antes do handler rodar.

---

## 4. Fluxo end-to-end

Exemplo: Maria quer pipeline para API `payments-api`.

1. Maria abre chat: "Quero criar uma pipeline"
2. Agente pergunta link do repo → valida via `validate_repo_url`
3. Pergunta branch → `validate_branch`
4. Pergunta namespace → `validate_namespace`
5. Pergunta tipo → `validate_app_type`
6. `propose_scaffold` → renderiza tudo, retorna lista de arquivos + colisoes
7. **CONFIRM**: agente mostra resumo: "Vou criar Dockerfile, azure-pipelines.yaml, helm/base/* e helm/environments/<env>/values.yaml no `payments-api`. Confirma?"
8. EXECUTE: clone shallow do repo da app → escreve arquivos → cria branch `scaffold/api-20260529-1430` → commit/push → abre PR no repo da app
9. Cleanup tmpfs
10. Agente entrega o link do PR no chat

**Tempo total:** 60–90s apos confirmacao. Latencia por turno: <2s.

---

## 5. Modelo de templates

### Estrutura
```
templates/
├── pipeline/        (azure-pipelines.yaml.j2)
├── dockerfile/      (api/worker/web Dockerfile.j2)
└── helm/
    ├── _common/     (Chart.yaml, values.yaml, _helpers.tpl, serviceaccount)
    ├── api/         (deployment HTTP probes, service, ingress, hpa)
    ├── worker/      (deployment exec probes, keda-scaledobject)
    └── web/         (deployment, service, ingress TLS, hpa)
```

### Pipeline YAML referencia template do DevOps por **tag**

```yaml
# stack dotnet (espelha o template-aks.yml de epharma-api-utils)
resources:
  repositories:
    - repository: templates
      type: git
      name: epharmaDevOps/AzurePipelinesTemplates
      ref: refs/heads/master   # stack dotnet referencia por BRANCH; _default usa refs/tags/<tag>

extends:
  template: pipelines/template-aks.yml@templates   # caminho por stack (DEVOPS_TEMPLATE_<LANG>)
  parameters:                                        # contrato por stack; ex. .NET:
    enableApim: true                                 # so api; substitui deployAKS/deployAppService
    applicationName: api-utils                       # perguntado no fluxo (default = nome do repo)
    namespace: corporativo
    deploymentStrategy: rolling
    pathApiManagementService: utils                  # perguntado (default = applicationName sem 'api-')
    apiId: api-utils                                 # perguntado (OBRIGATORIO, sem default; so api)
    ApplicationType: dotnet
    projectType: webapi
    dotNetVersion: 8.0.x
    imageName: $(Build.Repository.Name)              # macro do repo (modelo do time)
    imageTag: $(Build.SourceBranchName)-$(Build.BuildId)
```

O Helm vive no **repositorio da propria aplicacao**: o chart vai para `helm/base/` e os overrides
por ambiente para `helm/environments/<env>/values.yaml` (o template do time le os manifests do checkout
self e faz `helm upgrade -f` por ambiente). Nao ha PR no repo do time de DevOps.

**Modelo do time (AksHelmBaseCharts):** api/web/worker geram uma chart FINA que depende de uma base chart
compartilhada (microservice-chart/worker-chart) — sem templates proprios; os values ficam aninhados sob
a chave da base chart. cronjob mantem o chart self-contained. A tabela abaixo descreve as caracteristicas
conceituais por tipo (hoje materializadas pela base chart).

A chart fina **vendoriza a base chart empacotada**: alem de `Chart.yaml`/`values.yaml`, o agente emite
`helm/base/charts/<base>-<versao>.tgz` (binario) + `helm/base/Chart.lock` — par consistente, saidas reais
de `helm dependency build`, mantidas em `templates/helm/basechart/_vendor/<base>-<versao>.tgz`/`.lock`. O
`Chart.yaml` declara a dependency com `repository: file://.../AksHelmBaseCharts/...` (metadado); o `helm`
renderiza pelo `.tgz` em `charts/`, sem precisar do AksHelmBaseCharts no momento do deploy (validado com
`helm lint`/`helm template`). O override por ambiente (microservice) tem dois ingresses (primary/legacy) e
e gerado para todos os ambientes, incluindo `dev` (a referencia deixa dev vazio, mas o agente gera o
override para o usuario ajustar).

**Decisao critica:** o stack `_default` referencia o template por tag (evita que mudanca no template quebre
pipelines pinadas); o stack `dotnet` referencia por branch `master` para espelhar 1:1 o contrato real do time.

### Variacoes API/Worker/Web

| Aspecto | API | Worker | Web |
|---|---|---|---|
| Service k8s | ClusterIP :8080 | nenhum | ClusterIP :80 |
| Ingress | flag `expose_external` | nunca | sempre (TLS) |
| Probes | HTTP `/healthz` | exec | HTTP `/` |
| HPA | CPU/memory | KEDA (queue) | CPU |
| CMD | `api` | `worker` | `nginx -g 'daemon off;'` |

---

## 6. Seguranca e governanca

### ADR-001: Autenticacao Azure DevOps

**Decisao:** Service Principal dedicado (`sp-cicd-agent`) com secret em Key Vault + Managed Identity da Container App. **Rejeitado:** PAT por usuario (UX terrivel, vaza em log, devs sem permissao de criar PAT corp).

**Garantia de permissao do usuario humano:** antes de qualquer clone, agente chama `azdo.check_user_permission(repo, user.oid, 'Contribute')` usando token OBO **do usuario** (nao do SP). Se reprovar, recusa sem usar o SP — assim o SP nunca vira privilege escalation acidental.

**Auditoria:** todo PR contem `Generated by CI/CD Agent on behalf of: maria.santos@contoso.com (oid=...) session=abc123`. Log estruturado em App Insights.

### Por que PR e nunca commit direto
1. Branch protection em `main` ja bloquearia
2. Human-in-the-loop (dev e DevOps revisam)
3. Reversibilidade trivial
4. Auditoria natural (timeline, comentarios, reviewers)

### Outros controles
- **Allowlist** de orgs AzDO (`dev.azure.com/contoso/*` apenas)
- **Rate limit** 5 sessoes/hora por usuario
- **Sandbox FS**: clones so em `/dev/shm/sess-<id>/`; UID nao-root; read-only root FS
- **Sem execucao** de codigo clonado (nunca roda `npm install`, etc.)
- **Anti prompt-injection**: inputs do usuario em `<user_input>...</user_input>` como dados, nao instrucoes

---

## 7. Validacoes pre-geracao

| Validacao | Falha → |
|---|---|
| URL AzDO valido + na org allowlisted | pede correcao |
| Repo existe + SP tem acesso | "nao encontrei ou nao tenho acesso" |
| Usuario humano tem `Contribute` | "fale com o owner" |
| Branch existe | sugere alternativas |
| Namespace RFC 1123 (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, ≤63) | recusa |
| Namespace nao reservado (`kube-*`, `default`, `istio-*`) | recusa |
| `app_type` ∈ {api, worker, web} | recusa |
| Colisao `Dockerfile`/`azure-pipelines.yaml` no app | mostra diff, 3 opcoes |
| Colisao `helm/base/` ou `helm/environments/` no app | mesma coisa |
| Templates DevOps acessiveis na tag esperada | health check unhealthy |

---

## 8. Tratamento de erros e UX

| Erro | Tratamento |
|---|---|
| Validacao input | Repergunta com exemplo |
| Permissao negada | Mensagem clara + audit |
| Recurso inexistente | Oferece alternativas |
| Colisao | Diff + sobrescrever/sufixo/cancelar |
| Transitorio (5xx, 429) | Retry exponencial 3x |
| Bug interno | sessionId destacado para suporte |
| LLM rate limit | Circuit breaker + "tente em 1 min" |
| Abandono | TTL Redis 1h; se ja em EXECUTE, completa idempotente |

### Padroes UX
- **Sempre confirmar antes de executar** (estado CONFIRM)
- **Streaming token-by-token**
- **1 pergunta por turno**
- Comandos especiais: `/cancelar`, `/reiniciar`, `/status` interpretados antes do LLM
- **Idempotencia**: se branch `scaffold/...` ja existe, agente detecta e retoma
- **Tom**: diagnostico, nao acusacao. Sempre incluir `sessionId` em erros nao-recuperaveis

---

## 9. Estrategia pos-MVP

| Onda | Prazo | Escopo |
|---|---|---|
| **1 (MVP)** | 8–10 sem | AKS + ACR + Helm + 3 tipos |
| **2** | 3 meses | Deteccao automatica de linguagem; tipo `cronjob`; update de pipelines existentes; modo CLI |
| **3** | 6 meses | App Service, Functions, Container Apps; Bicep/Terraform; GitHub Actions |
| **4** | 12 meses | Multi-cloud (EKS/GKE) — provavelmente agente irmao, nao extensao |

**Principios de extensibilidade no MVP:**
1. Catalogo de tipos como **dado YAML**, nao codigo
2. Tools como **plugins** com interface estavel
3. State machine **parametrizavel** (trocar AKS por App Service ≠ mudar maquina)

---

## 10. Riscos e mitigacoes

| # | Risco | P | I | Mitigacao |
|---|---|---|---|---|
| R1 | Drift template DevOps ↔ agente | A | A | Smoke test diario; versionamento por tag |
| R2 | LLM gera YAML invalido | M | M | **Nao delegamos geracao ao LLM**; Jinja deterministico + Pydantic |
| R3 | Custo tokens explode | B | M | Rate limit; circuit breaker custo/dia; ~$50–150/mes para 500 sessoes |
| R4 | Lock-in Azure OpenAI | M | B | Interface `LLMClient`; trocar = 1–2 dias |
| R5 | Prompt injection via repo | M | A | **MVP nao le codigo do repo** |
| R6 | SP do agente comprometido | B | A | Scope minimo; branches `scaffold/*` apenas; rotation 90d; PR-only |
| R7 | DevOps perde controle ("shadow IT") | M | A | DevOps = dono dos templates + reviewer obrigatorio |
| R8 | Repos enormes estouram tmpfs | B | M | Shallow clone; limite 500MB; fallback azurefile |
| R9 | AzDO TSTU rate limit | M | B | Retry + cache metadata 5min |
| R10 | Redis perde sessao em EXECUTE | B | M | EXECUTE idempotente; cleanup job diario |

---

## 11. ADRs (resumo)

| # | Decisao |
|---|---|
| ADR-001 | SP dedicado + OBO check do usuario (nao PAT por usuario) |
| ADR-002 | UI = Teams bot; fallback SPA |
| ADR-003 | Container Apps, nao AKS |
| ADR-004 | State machine explicita, nao agent loop livre (LangGraph adiado) |
| ADR-005 | Templates Jinja2 deterministicos; LLM nao gera arquivos |
| ADR-006 | Pipeline YAML referencia template por **tag**, nao branch |
| ADR-007 | PR-only, sempre |
| ADR-008 | GPT-4.1 full (nao mini) |
| ADR-009 | Allowlist rigida de orgs AzDO |
| ADR-010 | MVP so AKS+Helm+ACR; extensivel por design |

---

## 12. Pontos em aberto

A serem fechados conforme o desenvolvimento avanca:

1. Templates do DevOps em **um repo versionado por tag**, ou varios? (Se varios → mover para mono-repo antes do MVP)
2. **Teams ou Slack** como UI principal?
3. Existe catalogo/API de namespaces oficiais do cluster?
4. SLA esperado (tier-1 oncall 24/7 vs tier-2)?

> Os pontos sobre Azure OpenAI (data residency, orcamento) cairam com a remocao da camada de LLM.
> O ponto sobre reviewers do `devops-templates` caiu: o Helm passou a viver no repo da propria app
> (PR unico, revisado pelo time dono da aplicacao).

---

## 13. Estado atual da implementacao

POC completa em [`src/devops_agent/`](../src/devops_agent/). **250 testes passando**.
A camada de LLM foi removida: o agente roda 100% deterministico.

**Modulos prontos:**
- `models/` — SessionState, Phase, AppType, PipelineRequest, RepoCoordinates, ToolResult, ExecutionResult
- `tools/validation.py` — validate_repo_url (allowlist), validate_k8s_namespace, validate_app_type, validate_branch_name
- `tools/scaffold.py` — build_bundle, dry_run com deteccao de colisao
- `tools/git_ops.py` — temp_workspace, clone_shallow, write_files, create_branch_and_commit, push_branch
- `tools/azdo.py` — AzdoClient (httpx) com retry exponencial
- `orchestrator/state_machine.py` — handlers das fases (state machine pura)
- `orchestrator/executor.py` — clone do repo da app + write + commit + push + PR
- `cli.py` — comandos `scaffold` e `chat` (mesmo fluxo deterministico; `chat` e alias)

**Implementado (modo de auth `sp`):**
- Service Principal via Entra ID (`AZDO_AUTH_MODE=sp`): app token por `ClientSecretCredential` (secret no Key Vault) ou `DefaultAzureCredential` (Managed Identity da Container App = o "WIF" do ADR; em AKS seria workload identity). Token usado para clone/push/PR (header `Authorization: Bearer`).
- `check_user_permission` via OBO antes de qualquer clone: troca a assercao do usuario (`OnBehalfOfCredential`) por um token Azure DevOps e avalia `Contribute` no repo (permissionevaluationbatch). O app se autentica no OBO via client secret OU, sem secret, via client assertion emitido pela Managed Identity (federated identity credential). Fail-closed: recusa sem usar o SP.
- Assercao do usuario obtida do front-end (`--user-token`/`AZDO_AGENT_USER_ASSERTION`) ou via login interativo device code (`AZDO_LOGIN_CLIENT_ID`).
- Auditoria "on behalf of" no corpo do PR.

**Pendente para producao:**
- Hosting em Azure Container Apps
- Teams bot / SPA front-end
- App Insights / OpenTelemetry
- Redis para sessao persistente
