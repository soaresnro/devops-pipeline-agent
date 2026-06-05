"""Schemas (JSON) das tools no formato OpenAI/Azure OpenAI function calling.

Cada tool corresponde a uma operacao tipada do dominio. O LLM nunca executa
codigo - apenas decide qual tool chamar e com quais argumentos. Toda a
geracao de artefatos (Dockerfile, YAML, Helm) e deterministica, feita pelas
implementacoes em devops_agent.tools.*
"""

from __future__ import annotations

from typing import Any

from devops_agent.models import AppType

_ACCEPTED_APP_TYPES = ", ".join(t.value for t in AppType)


# Helper: monta o envelope que a API espera.
def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def tools_for_agent() -> list[dict[str, Any]]:
    """Catalogo de tools expostas ao LLM para conduzir o fluxo conversacional."""
    return [
        _tool(
            name="validate_repo_url",
            description=(
                "Valida a URL de um repositorio Azure DevOps fornecida pelo usuario. "
                "Chame esta tool depois que o usuario responder a pergunta sobre o link do repo. "
                "Retorna as coordenadas (organization, project, repository) se valida; "
                "caso contrario, retorna ok=false com mensagem amigavel para repassar ao usuario."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL HTTPS de clone do Azure DevOps (https://dev.azure.com/<org>/<project>/_git/<repo>).",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="validate_branch",
            description=(
                "Valida o nome da branch onde os arquivos da pipeline devem ser adicionados. "
                "Chame depois que o usuario responder a segunda pergunta."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Nome da branch (ex.: main, master, develop).",
                    }
                },
                "required": ["branch"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="validate_namespace",
            description=(
                "Valida o nome do namespace Kubernetes. Aceita apenas letras minusculas, "
                "digitos e hifens (RFC 1123) e recusa namespaces reservados do sistema."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Namespace Kubernetes alvo (ex.: payments-prod).",
                    }
                },
                "required": ["namespace"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="validate_app_type",
            description=(
                "Valida o tipo da aplicacao informado pelo usuario. Os unicos tipos suportados "
                f"sao: {_ACCEPTED_APP_TYPES}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": f"Tipo informado pelo usuario ({_ACCEPTED_APP_TYPES}).",
                    }
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="propose_scaffold",
            description=(
                "Renderiza em memoria todos os arquivos que serao criados (Dockerfile, "
                "azure-pipelines.yaml, Helm chart) e detecta colisoes contra arquivos "
                "que ja existem no repo da aplicacao. Chame esta tool DEPOIS de ter validado "
                "as 4 respostas (repo, branch, namespace, tipo). Retorna uma lista resumida dos "
                "arquivos planejados, para voce mostrar ao usuario antes de pedir confirmacao."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        _tool(
            name="execute_scaffold",
            description=(
                "Executa o plano: clona os 2 repositorios, escreve arquivos, faz commit/push "
                "em branches novas e abre os Pull Requests. SOMENTE chame esta tool DEPOIS "
                "que o usuario confirmar explicitamente o resumo mostrado por propose_scaffold "
                "(resposta tipo 'sim', 'confirmo', 'pode criar'). NUNCA chame sem confirmacao."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_confirmation": {
                        "type": "string",
                        "description": "Texto exato com que o usuario confirmou (auditoria).",
                    }
                },
                "required": ["user_confirmation"],
                "additionalProperties": False,
            },
        ),
        _tool(
            name="abort_session",
            description=(
                "Cancela a sessao sem fazer nenhuma alteracao em repositorio algum. "
                "Use quando o usuario pedir para cancelar, desistir, ou quando ele recusar "
                "a confirmacao mostrada por propose_scaffold."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo curto (ex.: 'usuario_recusou_confirmacao', 'usuario_pediu_cancelar').",
                    }
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        ),
    ]


TOOL_NAMES: frozenset[str] = frozenset(
    t["function"]["name"] for t in tools_for_agent()
)
