"""Configuracao compartilhada dos testes.

Garante que os testes sejam HERMETICOS: o `Settings` (pydantic-settings) le um
`.env` relativo ao CWD por padrao; em dev o repo tem um `.env` real com valores
da org do usuario, que poluiria os testes. A fixture abaixo desabilita a leitura
do `.env` em todos os testes in-process. O e2e (test_cli_e2e) roda em subprocess
com `.env` proprio em tmp_path e nao e afetado.
"""

from __future__ import annotations

import pytest

from devops_agent.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
