"""Configuration loading and the benchmark home layout.

A *home* is a directory containing::

    config/repos.json
    config/tools.json
    config/tasks.json
    manifest.json
    results/
    work/
    cache/gh/

The default home is the directory that contains ``bench.py``. Override it with
``--home`` or the ``BENCH_HOME`` environment variable, which makes runs and
tests isolated.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path(__file__).resolve().parent.parent


@dataclass
class BenchHome:
    root: Path
    config_dir: Path = field(init=False)
    results_dir: Path = field(init=False)
    work_dir: Path = field(init=False)
    cache_dir: Path = field(init=False)
    manifest_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.config_dir = self.root / "config"
        self.results_dir = self.root / "results"
        self.work_dir = self.root / "work"
        self.cache_dir = self.root / "cache"
        self.manifest_path = self.root / "manifest.json"

    @classmethod
    def resolve(cls, value: str | os.PathLike | None = None) -> "BenchHome":
        root = value or os.environ.get("BENCH_HOME") or DEFAULT_HOME
        return cls(Path(root))

    # ------------------------------------------------------------- lifecycle
    def ensure_dirs(self) -> None:
        for path in (self.config_dir, self.results_dir, self.work_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- configs
    def repos_path(self) -> Path:
        return self.config_dir / "repos.json"

    def tools_path(self) -> Path:
        return self.config_dir / "tools.json"

    def tasks_path(self) -> Path:
        return self.config_dir / "tasks.json"

    def load_repos(self) -> dict:
        return load_json(self.repos_path(), {"version": 1, "repos": {}})

    def load_tools(self) -> dict:
        return load_json(self.tools_path(), {"version": 1, "tools": {}})

    def load_tasks(self) -> dict:
        return load_json(self.tasks_path(), {"version": 1, "tasks": {}})

    def load_manifest(self) -> dict:
        return load_json(self.manifest_path, {"version": 1, "instances": []})

    def save_repos(self, payload: dict) -> None:
        save_json(self.repos_path(), payload)

    def save_tools(self, payload: dict) -> None:
        save_json(self.tools_path(), payload)

    def save_tasks(self, payload: dict) -> None:
        save_json(self.tasks_path(), payload)

    def save_manifest(self, payload: dict) -> None:
        save_json(self.manifest_path, payload)

    def repo_path(self, repo_cfg: dict) -> Path:
        raw = Path(str(repo_cfg.get("path", ""))).expanduser()
        if raw.is_absolute():
            return raw
        return (self.root / raw).resolve()


def load_json(path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}")


def save_json(path, payload: Any) -> None:
    """Atomically write JSON (avoids truncated configs on interruption)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.chmod(temp_name, 0o644)
        except OSError:
            pass
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def enabled_names(section: dict, key: str = "enabled") -> list[str]:
    return [
        name for name, cfg in (section or {}).items()
        if bool((cfg or {}).get(key, True))
    ]


def select_config(
    section: dict,
    requested: list[str] | None = None,
    *,
    include_disabled: bool = False,
) -> dict:
    """Select entries by name, honouring ``enabled`` unless explicitly named."""
    if not section:
        return {}
    if requested:
        missing = [name for name in requested if name not in section]
        if missing:
            raise SystemExit(
                f"unknown entries: {missing}; known: {sorted(section)}"
            )
        return {
            name: section[name] for name in requested
            if include_disabled or bool(section[name].get("enabled", True))
        }
    return {
        name: cfg for name, cfg in section.items()
        if include_disabled or bool((cfg or {}).get("enabled", True))
    }
