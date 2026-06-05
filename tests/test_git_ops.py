"""Testes das tools de Git contra um bare repo local que simula o remote AzDO.

Permite validar clone+write+commit+push ponta-a-ponta sem credenciais reais.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devops_agent.tools.git_ops import (
    clone_shallow,
    create_branch_and_commit,
    list_existing_paths,
    push_branch,
    temp_workspace,
    write_files,
)
from devops_agent.tools.scaffold import ScaffoldedFile


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


@pytest.fixture
def fake_remote(tmp_path: Path) -> Path:
    """Cria um bare repo (origin) com 1 commit inicial em 'main'."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--initial-branch=main")

    # Seed: clona, faz primeiro commit, push
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.email", "seed@test")
    _git(seed, "config", "user.name", "seed")
    (seed / "README.md").write_text("seed", encoding="utf-8")
    (seed / "src").mkdir()
    (seed / "src" / "main.cs").write_text("// app", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", bare.as_uri())
    _git(seed, "push", "-u", "origin", "main")
    return bare


class TestGitOps:
    def test_workspace_e_efemero(self) -> None:
        captured_path: list[Path] = []
        with temp_workspace() as ws:
            captured_path.append(ws.root)
            assert ws.root.exists()
        assert not captured_path[0].exists()

    def test_clone_shallow_funciona_contra_remote_local(self, fake_remote: Path) -> None:
        with temp_workspace() as ws:
            result = clone_shallow(
                ws,
                key="app",
                clone_url=fake_remote.as_uri(),
                branch="main",
            )
            assert result.ok, result.error
            assert result.data is not None
            assert (result.data / "README.md").read_text() == "seed"
            # Configuracao de identidade aplicada localmente
            assert "DevOps Pipeline Agent" in _git(result.data, "config", "user.name")

    def test_list_existing_paths_retorna_arquivos_versionados(self, fake_remote: Path) -> None:
        with temp_workspace() as ws:
            r = clone_shallow(
                ws, key="app", clone_url=fake_remote.as_uri(), branch="main"
            )
            assert r.ok
            assert r.data is not None
            paths = list_existing_paths(r.data)
            assert paths == {"README.md", "src/main.cs"}

    def test_clone_de_branch_inexistente_falha(self, fake_remote: Path) -> None:
        with temp_workspace() as ws:
            r = clone_shallow(
                ws,
                key="app",
                clone_url=fake_remote.as_uri(),
                branch="nonexistent",
            )
            assert not r.ok
            assert r.error_code == "git.clone_failed"

    def test_ciclo_completo_clone_write_commit_push(self, fake_remote: Path) -> None:
        files = [
            ScaffoldedFile(path="Dockerfile", content="FROM scratch\n"),
            ScaffoldedFile(
                path="azure-pipelines.yaml",
                content="trigger:\n  branches:\n    include:\n      - main\n",
            ),
        ]

        with temp_workspace() as ws:
            cr = clone_shallow(
                ws, key="app", clone_url=fake_remote.as_uri(), branch="main"
            )
            assert cr.ok
            assert cr.data is not None

            wr = write_files(cr.data, files)
            assert wr.ok and wr.data == 2

            br = create_branch_and_commit(
                cr.data,
                new_branch="scaffold/test-1",
                commit_message="chore(ci): scaffold via agent",
            )
            assert br.ok, br.error
            assert br.data is not None
            assert len(br.data) == 40  # SHA-1 hex

            pr = push_branch(cr.data, branch="scaffold/test-1")
            assert pr.ok, pr.error

        # Verifica que o push chegou no remote
        check = tmp_check = Path(str(fake_remote)).parent / "verify"
        tmp_check.mkdir(exist_ok=True)
        _git(check, "clone", "--branch", "scaffold/test-1", fake_remote.as_uri(), "verify-clone")
        verify_repo = check / "verify-clone"
        assert (verify_repo / "Dockerfile").read_text() == "FROM scratch\n"
        assert "azure-pipelines.yaml" in _git(
            verify_repo, "ls-tree", "-r", "--name-only", "HEAD"
        )

    def test_commit_sem_mudancas_falha(self, fake_remote: Path) -> None:
        with temp_workspace() as ws:
            cr = clone_shallow(
                ws, key="app", clone_url=fake_remote.as_uri(), branch="main"
            )
            assert cr.ok
            assert cr.data is not None
            br = create_branch_and_commit(
                cr.data,
                new_branch="scaffold/empty",
                commit_message="vazio",
            )
            assert not br.ok
            assert br.error_code == "git.nothing_to_commit"
