# Guia de uso - devops-pipeline-agent

Passo a passo para configurar o ambiente local e usar o agente que gera
`Dockerfile` + `azure-pipelines.yaml` + Helm (`helm/base` + `helm/environments`)
no repositorio da propria aplicacao, abrindo um PR.

> O agente e uma **CLI deterministica** (sem LLM): faz perguntas fixas e gera os
> arquivos por templates. A saida e reproduzivel e auditavel.

---

## 1. Pre-requisitos

| Ferramenta | Versao | Para que |
|---|---|---|
| **Python** | 3.12 ou superior | rodar o agente |
| **Git** | qualquer recente | clonar o repo da app e o proprio agente |
| **PAT do Azure DevOps** | escopo **Code (Read & Write)** | detectar linguagem, clonar e abrir o PR |

Nao e necessario Helm nem o GitHub CLI para **usar** o agente (sao so para
desenvolver este repo).

### Instalar o Python (Windows)

Se `python --version` no PowerShell abrir a Microsoft Store ou disser
"Python was not found", voce **nao** tem o Python real instalado (e so o atalho da Store).
Instale via winget:

```powershell
winget install --id Python.Python.3.12 -e --source winget
```

Depois **feche e reabra o terminal** para o PATH atualizar. Confirme:

```powershell
python --version    # deve mostrar Python 3.12.x
```

---

## 2. Clonar o agente

```powershell
cd C:\repositorios          # ou onde voce guarda seus repos
git clone https://github.com/soaresnro/devops-pipeline-agent.git
cd devops-pipeline-agent
```

> Se o time hospedar o agente em outro remote (ex.: Azure DevOps interno), use a URL de la.

---

## 3. Criar o ambiente virtual e instalar

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Se o `Activate.ps1` for bloqueado pela Execution Policy do PowerShell, rode uma vez:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

e ative de novo. Alternativa sem ativar: chame o binario direto em cada comando
(`.venv\Scripts\devops-agent ...`).

Confirme a instalacao:

```powershell
devops-agent version
```

---

## 4. Configurar o `.env`

Copie o exemplo e edite:

```powershell
copy .env.example .env
notepad .env
```

Preencha (valores do time epharma):

| Variavel | Valor | Observacao |
|---|---|---|
| `AZDO_ORG` | `epharmapbm` | allowlist - so repos desta org sao aceitos |
| `AZDO_AUTH_MODE` | `pat` | modo dev/uso; `sp` e so para producao |
| `AZDO_PAT` | *(seu PAT)* | **escopo Code (Read & Write)** - ver abaixo |
| `DEVOPS_TEMPLATES_REPO_URL` | `https://epharmapbm.visualstudio.com/epharmaDevOps/_git/AzurePipelinesTemplates` | repo dos templates de pipeline |
| `DEVOPS_TEMPLATES_BRANCH` | `master` | branch do template-aks (stack dotnet) |
| `DEVOPS_TEMPLATE_DOTNET` | `pipelines/template-aks.yml` | template real do time para .NET |
| `DEVOPS_ENVIRONMENTS` | `dev,qa,uat,prod` | ambientes gerados em `helm/environments/<env>` |

As demais chaves (`AZDO_SP_*`, `AZDO_LOGIN_CLIENT_ID`, etc.) so importam no modo `sp`
(producao) - deixe em branco.

### Como gerar o PAT

1. No Azure DevOps (`https://epharmapbm.visualstudio.com`), clique no seu perfil >
   **Personal access tokens**.
2. **New Token** > Organization: `epharmapbm`.
3. Em **Scopes**, marque **Code** com **Read & Write**.
4. Copie o token e cole em `AZDO_PAT=` no `.env`.

> O `.env` e local e **nao vai para o git** (esta no `.gitignore`). Nunca comite seu PAT.

---

## 5. Rodar o agente

```powershell
devops-agent scaffold
```

O agente faz as perguntas em sequencia. Exemplo (aplicacao .NET tipo API):

| Pergunta | Exemplo de resposta |
|---|---|
| Link do repositorio | `https://epharmapbm.visualstudio.com/Squad%20Engenharia/_git/minha-api` |
| Branch | `develop` |
| Namespace | `corporativo` |
| Tipo (api/worker/web/cronjob) | `api` |
| Linguagem¹ | `dotnet` |
| Versao .NET | *(enter usa 8.0.x)* |
| Assembly de entrada (.dll) | `MinhaApi.Services.dll` |
| applicationName | `api-minha` *(enter usa o nome do repo)* |
| pathApiManagementService | *(enter deriva de applicationName)* |
| **apiIds do APIM** | `api-minha` *(obrigatorio; um valor unico vale para dev/qa/uat/prod)* — ou os quatro por virgula na ordem dev,qa,uat,prod: `api-minha-dev,api-minha-qa,api-minha-uat,api-minha` |
| Host base do ingress | *(enter deriva de `services<path>`)* |
| **confirma? (sim/nao)** | ver abaixo |

¹ Com o PAT configurado, o agente detecta a linguagem sozinho e pula essa pergunta.

### Dry-run (seguro) vs execucao real

- **`nao`** no confirma = **dry-run**: o agente so mostra a lista de arquivos que
  criaria e encerra. **Nada e clonado, escrito ou enviado.** Use para conferir antes.
- **`sim`** = **execucao real**: o agente clona o repo da app, escreve os arquivos,
  cria uma branch `scaffold/...`, faz push e **abre um PR**. Ao final mostra o link do PR.

> Recomendado: rode uma vez com `nao` para revisar, depois `sim` para valer.

---

## 6. O que e gerado

No PR aberto no repositorio da **sua aplicacao**:

```
Dockerfile
azure-pipelines.yaml
helm/
  base/
    Chart.yaml
    Chart.lock
    values.yaml
    charts/microservice-chart-<versao>.tgz
  environments/
    dev/values.yaml
    qa/values.yaml
    uat/values.yaml
    prod/values.yaml
```

Depois do merge, ainda e preciso **criar a definicao da Pipeline** no Azure DevOps
apontando para o `azure-pipelines.yaml` (o agente entrega os arquivos, mas nao cria a
Pipeline nem o cluster).

---

## 7. Problemas comuns

| Sintoma | Causa / solucao |
|---|---|
| "Python was not found" / abre a Store | So tem o stub da Store. Instale o Python real (passo 1) e reabra o terminal. |
| `Activate.ps1` bloqueado | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (passo 3). |
| "Organizacao ... nao esta na allowlist" | A URL do repo nao e da org de `AZDO_ORG`. Use um repo em `epharmapbm`. |
| Pergunta a linguagem em vez de detectar | `AZDO_PAT` vazio ou sem escopo Code Read. Configure o PAT (passo 4). |
| "Arquivos ja existem no repo da app" | O repo-alvo ja tem `Dockerfile`/`helm/`. O agente aborta para nao sobrescrever - remova/renomeie os existentes ou use outro repo. |
| Nao abriu PR, so fez push da branch | Sem credencial para a API (PAT ausente/invalido). Confira `AZDO_PAT`. |

---

## Resumo rapido (TL;DR)

```powershell
winget install --id Python.Python.3.12 -e --source winget   # se nao tiver Python
git clone https://github.com/soaresnro/devops-pipeline-agent.git
cd devops-pipeline-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env        # editar: AZDO_ORG, AZDO_PAT, DEVOPS_TEMPLATES_*
devops-agent scaffold         # responder 'nao' para dry-run; 'sim' para abrir PR
```
