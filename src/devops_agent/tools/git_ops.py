"""Tools de Git: clone shallow, write files, commit e push.

Toda operacao acontece em um workdir efemero (tmpdir) que e descartado ao fim
do contexto. O agente nunca executa codigo do repo clonado.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from devops_agent.models import ToolResult
from devops_agent.tools.scaffold import ScaffoldedFile

logger = logging.getLogger(__name__)

_GIT_USER_NAME = "DevOps Pipeline Agent"
_GIT_USER_EMAIL = "devops-agent@noreply.local"
_DEFAULT_TIMEOUT_SEC = 120


class GitError(Exception):
    """Erro durante operacao git - capturado pelas tools, nunca propagado ao LLM."""


@dataclass(frozen=True)
class GitAuth:
    """Credencial para chamadas git over HTTPS contra Azure DevOps.

    Para PAT: username pode ser vazio ou 'pat'; password = PAT.
    Para OAuth/Bearer: username vazio; password = token.
    """

    username: str
    password: str

    def to_basic_header(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()


class GitWorkspace:
    """Workspace efemero contendo um ou mais clones."""

    def __init__(self, root: Path):
        self.root = root
        self.clones: dict[str, Path] = {}

    def path_for(self, key: str) -> Path:
        return self.clones[key]


@contextmanager
def temp_workspace(root: Path | None = None) -> Iterator[GitWorkspace]:
    """Context manager que cria um workdir e garante cleanup."""
    parent = root or Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    workdir = parent / f"agent-{uuid.uuid4().hex[:12]}"
    workdir.mkdir(parents=True, exist_ok=False)
    ws = GitWorkspace(workdir)
    try:
        yield ws
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError as exc:
            logger.warning("Falha removendo workdir %s: %s", workdir, exc)


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SEC,
) -> str:
    """Executa git com timeout e captura stdout/stderr. Levanta GitError em falha."""
    import os

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # nunca prompta credencial
    if extra_env:
        env.update(extra_env)

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git nao esta instalado ou nao esta no PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timeout apos {timeout}s: {' '.join(args)}") from exc

    if completed.returncode != 0:
        raise GitError(
            f"git falhou (exit={completed.returncode}): {' '.join(args)}\n"
            f"stderr: {completed.stderr.strip()}"
        )
    return completed.stdout


def _build_auth_args(auth: GitAuth | None) -> list[str]:
    """Injeta header de Authorization via -c http.extraHeader."""
    if auth is None:
        return []
    return ["-c", f"http.extraHeader=Authorization: {auth.to_basic_header()}"]


def clone_shallow(
    workspace: GitWorkspace,
    *,
    key: str,
    clone_url: str,
    branch: str,
    auth: GitAuth | None = None,
) -> ToolResult[Path]:
    """Clone shallow (--depth 1) de uma branch especifica."""
    target = workspace.root / key
    if target.exists():
        return ToolResult.failure(
            f"Diretorio de clone ja existe: {target}",
            code="git.clone_target_exists",
        )

    args = [
        *_build_auth_args(auth),
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        branch,
        clone_url,
        str(target),
    ]
    try:
        _run_git(args, timeout=180)
    except GitError as exc:
        return ToolResult.failure(str(exc), code="git.clone_failed")

    # Configura identidade local (nao polui config global)
    try:
        _run_git(["config", "user.name", _GIT_USER_NAME], cwd=target)
        _run_git(["config", "user.email", _GIT_USER_EMAIL], cwd=target)
    except GitError as exc:
        return ToolResult.failure(str(exc), code="git.config_failed")

    workspace.clones[key] = target
    return ToolResult.success(target)


def list_existing_paths(repo_path: Path) -> set[str]:
    """Lista paths versionados em HEAD (relativos a raiz) para deteccao de colisao."""
    out = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_path)
    return {line.strip() for line in out.splitlines() if line.strip()}


def write_files(repo_path: Path, files: list[ScaffoldedFile]) -> ToolResult[int]:
    """Escreve uma lista de ScaffoldedFile no repo (criando diretorios)."""
    count = 0
    try:
        for f in files:
            full = repo_path / Path(f.path)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f.content, encoding="utf-8")
            count += 1
    except OSError as exc:
        return ToolResult.failure(
            f"Falha escrevendo arquivo: {exc}", code="git.write_failed"
        )
    return ToolResult.success(count)


def create_branch_and_commit(
    repo_path: Path,
    *,
    new_branch: str,
    commit_message: str,
) -> ToolResult[str]:
    """Cria branch a partir do HEAD atual, stage all e commit. Retorna SHA."""
    try:
        _run_git(["checkout", "-b", new_branch], cwd=repo_path)
        _run_git(["add", "--all"], cwd=repo_path)
        # Verifica se ha mudancas para commitar
        status = _run_git(["status", "--porcelain"], cwd=repo_path).strip()
        if not status:
            return ToolResult.failure(
                "Nenhuma mudanca para commitar (arquivos identicos aos existentes).",
                code="git.nothing_to_commit",
            )
        _run_git(["commit", "-m", commit_message], cwd=repo_path)
        sha = _run_git(["rev-parse", "HEAD"], cwd=repo_path).strip()
    except GitError as exc:
        return ToolResult.failure(str(exc), code="git.commit_failed")
    return ToolResult.success(sha)


def push_branch(
    repo_path: Path,
    *,
    branch: str,
    auth: GitAuth | None = None,
) -> ToolResult[str]:
    """Push da branch para origin."""
    args = [*_build_auth_args(auth), "push", "--set-upstream", "origin", branch]
    try:
        _run_git(args, cwd=repo_path, timeout=180)
    except GitError as exc:
        return ToolResult.failure(str(exc), code="git.push_failed")
    return ToolResult.success(branch)
