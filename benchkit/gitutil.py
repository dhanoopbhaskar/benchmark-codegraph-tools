"""Git helpers used by mining, worktrees and doctor."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .process import run_cmd


def git(repo, *args, timeout: float = 180, check: bool = True) -> str:
    rc, out, err, _ = run_cmd(["git", "-C", str(repo), *args], timeout=timeout)
    if check and rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err.strip()[:600]}")
    return out


def is_git_repo(path) -> bool:
    repo = Path(path)
    if not repo.exists():
        return False
    rc, _, _, _ = run_cmd(["git", "-C", str(repo), "rev-parse", "--git-dir"], timeout=30)
    return rc == 0


def head_commit(repo) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def repo_slug(repo) -> str | None:
    try:
        url = git(repo, "remote", "get-url", "origin").strip()
    except Exception:
        return None
    match = re.search(
        r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?$", url
    )
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('name')}"


def commit_exists(repo, commit: str) -> bool:
    rc, _, _, _ = run_cmd(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], timeout=30
    )
    return rc == 0


def file_exists_at(repo, commit: str, path: str) -> bool:
    rc, _, _, _ = run_cmd(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}:{path}"], timeout=30
    )
    return rc == 0


def show_file(repo, commit: str, path: str, timeout: float = 60) -> str:
    rc, out, err, _ = run_cmd(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"], timeout=timeout
    )
    if rc != 0:
        raise RuntimeError(f"git show {commit}:{path} failed: {err.strip()[:300]}")
    return out


def list_tree(repo, commit: str, timeout: float = 120) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", commit, timeout=timeout)
    return [line for line in out.splitlines() if line.strip()]


def log_records(repo, since: str, timeout: float = 300):
    """Yield ``(sha, timestamp, subject, body)`` newest-first."""
    out = git(
        repo,
        "log",
        "--no-merges",
        f"--since={since}",
        "--format=%H%x1f%ct%x1f%s%x1f%b%x1e",
        timeout=timeout,
    )
    records = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 3:
            continue
        records.append(
            (parts[0], int(parts[1]), parts[2], parts[3] if len(parts) > 3 else "")
        )
    records.sort(key=lambda row: (-row[1], row[0]))
    return records


def diff_numstat(repo, base: str, target: str, timeout: float = 120) -> str:
    return git(
        repo, "diff", "--numstat", "--no-renames", base, target, timeout=timeout
    )


def diff_hunks(repo, base: str, target: str, paths: list[str], timeout: float = 120) -> str:
    if not paths:
        return ""
    return git(
        repo,
        "diff",
        "--unified=0",
        "--no-renames",
        base,
        target,
        "--",
        *paths,
        timeout=timeout,
    )


def make_worktree(repo, commit: str, dest) -> Path:
    """Create a detached worktree at *commit*, replacing a stale destination."""
    repo = Path(repo)
    dest = Path(dest)
    if dest.exists():
        remove_worktree(repo, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err, _ = run_cmd(
        ["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(dest), commit],
        timeout=900,
    )
    if rc != 0:
        raise RuntimeError(f"worktree add failed: {err.strip()[-600:]}")
    return dest


def remove_worktree(repo, dest) -> None:
    repo = Path(repo)
    dest = Path(dest)
    run_cmd(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(dest)],
        timeout=180,
    )
    shutil.rmtree(dest, ignore_errors=True)
    run_cmd(["git", "-C", str(repo), "worktree", "prune"], timeout=60)


def prune_worktrees(repo) -> None:
    run_cmd(["git", "-C", str(repo), "worktree", "prune"], timeout=120)
