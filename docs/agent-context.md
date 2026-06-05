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

## Estado do projeto (atualizado em 2026-06-05)

MVP/POC completo e verde: **247 testes**, `mypy --strict` limpo. O repo do Helm e configuravel e
separado do repo de templates (`DEVOPS_HELM_REPO_URL` + `DEVOPS_HELM_REPO_BRANCH`, default `main`).
`validate_repo_url` aceita
os formatos `dev.azure.com/<org>/...` e o legado `<org>.visualstudio.com/<projeto>/_git/<repo>`
(org = subdominio). A allowlist `AZDO_ORG` compara com a org extraida da URL.

**Adaptacao a templates reais do time (concluida para .NET):** (PR1) o bloco `extends.parameters` da
pipeline e por stack (`templates/pipeline/<stack>/azure-pipelines.yaml.j2`); `dotnet` usa o contrato
real (applicationName/ApplicationType/dotNetVersion/deployAKS=true/deployAppService=false; versao
perguntada no fluxo, default 8.0.x); node/python/go/web seguem no `_default` generico ate recebermos
os templates reais. (PR2) o Helm passou a ser gerado em `helm/apps/<app>` + override por ambiente em
`environments/<env>/<app>/values.yaml` (ambientes via `DEVOPS_ENVIRONMENTS`); os manifests nao fixam
mais `metadata.namespace` (o `helm -n <ns>-<env>` do template decide). Falta, conforme os templates
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
pip install -e ".[dev]"      # ou ".[llm,dev]" para o modo chat
copy .env.example .env       # preencher AZDO_* (e AZURE_OPENAI_* para chat)
gh auth login                # refazer login do gh
pytest                       # deve ficar verde
```

`.env` e `.venv` sao locais (gitignored / nao versionados) e precisam ser recriados.

## Testar o fluxo contra um Azure DevOps real (resumo)

Use `AZDO_AUTH_MODE=pat` (o modo `sp` exige app registrations + OBO; e producao).
PAT com escopo **Code (Read & Write)**. Deixe `DEVOPS_TEMPLATES_REVIEWERS` vazio
(o codigo manda reviewer como `{"id": ...}` e um nome simples nao e GUID valido).

1. `devops-agent scaffold` faz as perguntas, mostra o resumo e pergunta `confirma?`.
   Responder `nao` = dry-run seguro (nada e escrito). `sim` = abre os 2 PRs
   (app: Dockerfile + azure-pipelines.yaml; devops-templates: Helm chart).
2. Para a pipeline **compilar/rodar**, o template referenciado precisa existir no
   repo `devops-templates` **na tag** `DEVOPS_TEMPLATES_TAG` (referencia por tag,
   nunca branch). O caminho e escolhido pela linguagem via `DEVOPS_TEMPLATE_<LINGUAGEM>`
   (fallback `DEVOPS_TEMPLATE_DEFAULT=pipelines/k8s-app.yaml`; web usa o de node). Um
   stub que so ecoa os parametros ja roda verde sem AKS/ACR.
3. Criar a *definicao* da Pipeline no Azure DevOps apontando para `azure-pipelines.yaml`
   (o agente entrega os arquivos via PR, mas nao cria a Pipeline nem o cluster).
