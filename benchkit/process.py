"""Cross-platform subprocess helpers.

Every external command in the harness goes through :func:`run_cmd`, which:
  * runs argv as a list (never a shell string),
  * forces UTF-8 with replacement so odd tool output cannot crash the run,
  * kills the whole process group/tree on timeout,
  * returns wall-clock seconds alongside stdout/stderr.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Iterable, Mapping, Optional, Sequence


def run_cmd(
    argv: Sequence[object],
    cwd: Optional[object] = None,
    timeout: Optional[float] = 120,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[int, str, str, float]:
    """Run a command and return ``(returncode, stdout, stderr, elapsed_seconds)``.

    Raises :class:`TimeoutError` after killing the process tree.
    """
    merged_env = os.environ.copy()
    merged_env.setdefault("NO_COLOR", "1")
    merged_env.setdefault("FORCE_COLOR", "0")
    merged_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

    kwargs: dict = {
        "cwd": str(cwd) if cwd else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": merged_env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    started = time.perf_counter()
    try:
        proc = subprocess.Popen([str(a) for a in argv], **kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found: {argv[0]}") from exc
    try:
        out, err = proc.communicate(timeout=timeout or None)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        out, err = proc.communicate()
        raise TimeoutError(
            f"timed out after {timeout}s: {' '.join(str(a) for a in argv)[:400]}"
        )
    return proc.returncode, out or "", err or "", time.perf_counter() - started


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort kill of a process and its children on POSIX and Windows."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def which(binary: str) -> Optional[str]:
    """Cross-platform ``which`` that understands executable extensions."""
    if not binary:
        return None
    return shutil.which(str(binary))


def first_token(argv: Iterable[object]) -> str:
    for token in argv:
        value = str(token).strip()
        if value and not value.startswith("-"):
            return value
    return ""


def binary_for(cfg: Mapping, *keys: str) -> str:
    """Find the executable a tool config will invoke."""
    explicit = cfg.get("binary")
    if explicit:
        return str(explicit)
    for key in keys:
        value = cfg.get(key)
        if isinstance(value, (list, tuple)) and value:
            return first_token(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    version = cfg.get("version")
    if isinstance(version, (list, tuple)) and version:
        return first_token(version)
    return ""
