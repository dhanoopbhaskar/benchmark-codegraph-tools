"""Tool adapters: render a command, run it, parse results."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from .parsers import PARSERS, parse_results
from .plugins import resolve as resolve_plugin
from .process import run_cmd
from .runners import get_runner
from .text import (
    keywords, normalize_path, path_in_scope, render, split_terms, trim_problem,
)


def normalize_tool(tool_cfg: dict, tool_defaults: dict | None = None) -> dict:
    cfg = dict(tool_defaults or {})
    cfg.update(tool_cfg or {})
    return cfg


def query_for_task(tool_cfg: dict, task_name: str, task_cfg: dict) -> dict | None:
    """Return a normalised query config for a task, or ``None`` if unsupported."""
    queries = tool_cfg.get("queries") or {}
    raw = queries.get(task_name)
    if raw is None and task_cfg.get("allow_default_query"):
        raw = tool_cfg.get("query")
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = {"argv": list(raw)}
    elif isinstance(raw, str):
        raw = {"argv": [raw]}
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid query config for task '{task_name}'")
    resolved = dict(raw)
    for key in ("parse", "runner", "timeout", "cwd", "env", "scope_filter"):
        if resolved.get(key) is None and tool_cfg.get(key) is not None:
            resolved[key] = tool_cfg[key]
    resolved.setdefault("parse", "recursive_json")
    return resolved


def supports_task(tool_cfg: dict, task_name: str, task_cfg: dict) -> bool:
    return query_for_task(tool_cfg, task_name, task_cfg) is not None


def _include_basename(repo_cfg: dict) -> str:
    """Return the basename glob of the first include pattern (``**/*.java`` -> ``*.java``)."""
    for pattern in repo_cfg.get("include") or []:
        value = str(pattern).replace("\\", "/").rstrip("/")
        if value:
            return value.rsplit("/", 1)[-1]
    return ""


_SOURCE_ROOTS = {"java", "kotlin", "scala", "groovy"}


def _package_path(path: str) -> str:
    """Return the source-relative package path for a file.

    ``.../src/main/java/org/example/dao/Foo.java`` -> ``org/example/dao``.
    Falls back to the file's directory for layouts without a known source root.
    """
    parts = Path(path).parts
    for index, part in enumerate(parts):
        if (
            part in _SOURCE_ROOTS
            and index >= 2
            and parts[index - 1] in {"main", "test"}
            and parts[index - 2] == "src"
        ):
            return "/".join(parts[index + 1:-1])
    parent = Path(path).parent.as_posix() if path else ""
    return "" if parent == "." else parent


def _tool_query_text(instance: dict) -> str:
    query = instance.get("query") or {}
    return (
        query.get("issue_text")
        or query.get("text")
        or instance.get("problem_statement")
        or instance.get("title")
        or ""
    )


def build_context(
    instance: dict,
    task_name: str,
    task_cfg: dict,
    tool_cfg: dict,
    args,
    repo_path,
    repo_cfg: dict,
) -> dict:
    query_payload = instance.get("query") or {}
    issue_text = _tool_query_text(instance)
    task_source = task_cfg.get("query_source", "structural")

    if task_source == "issue":
        max_chars = int(getattr(args, "max_query_chars", 500) or 500)
        source = instance.get("problem_statement") or issue_text
        if getattr(args, "query_mode", "issue") == "title":
            base_query = instance.get("title") or source
        else:
            base_query = trim_problem(source)[:max_chars]
    elif task_source == "title":
        base_query = instance.get("title") or issue_text
    else:
        base_query = query_payload.get("text") or issue_text

    keyword_text = base_query
    if task_source == "structural" and query_payload.get("symbol"):
        keyword_text = query_payload["symbol"]
    symbols, words = split_terms(keyword_text)
    max_terms = int(task_cfg.get("max_keyword_terms", 12))
    flat_keywords = keywords(keyword_text, limit=max_terms)

    if tool_cfg.get("query_mode") == "keywords":
        rendered_query = " ".join(flat_keywords)
    else:
        rendered_query = base_query

    symbol = query_payload.get("symbol") or ""
    if symbol and not symbols:
        symbols = [symbol]
    if symbol and not words:
        words = []
    terms = (symbols[:8] + words[:4]) or flat_keywords or [str(symbol or base_query)[:40]]
    name_symbols = symbols[:8] or ([symbol] if symbol else [])
    name_pattern = ".*(" + "|".join(
        re.escape(str(term)) for term in name_symbols
    ) + ").*" if name_symbols else ".*"

    path = query_payload.get("path") or query_payload.get("file") or ""
    qualified = query_payload.get("qualified_symbol") or ""
    if not qualified and symbol and path:
        qualified = f"{Path(path).stem}.{symbol}"

    return {
        "repo": str(repo_path),
        "repo_name": instance.get("repo", ""),
        "run_id": getattr(args, "run_id", "") or "",
        "instance_id": instance.get("instance_id", ""),
        "task": task_name,
        "query": rendered_query,
        "raw_query": base_query,
        "issue_text": issue_text,
        "title": instance.get("title", ""),
        "k": int(getattr(args, "top_k", 10)),
        "top_k": int(getattr(args, "top_k", 10)),
        "symbol": symbol,
        "qualified_symbol": qualified,
        "symbol_kind": query_payload.get("symbol_kind", ""),
        "symbol_path": query_payload.get("path") or query_payload.get("file") or "",
        "symbol_line": query_payload.get("symbol_line", ""),
        "path": path,
        "file": path,
        "path_basename": Path(path).name if path else "",
        "path_stem": Path(path).stem if path else "",
        "path_dir": Path(path).parent.name if path else "",
        "path_package": _package_path(path) if path else "",
        "language": query_payload.get("language", ""),
        "source": query_payload.get("source") or "",
        "target": query_payload.get("target") or "",
        "keywords": " ".join(terms),
        "keywords_regex": "|".join(
            str(term).replace("\\", "\\\\") for term in terms
        ),
        "name_pattern": name_pattern,
        "impact_depth": int(task_cfg.get("impact_depth", 2)),
        "depth": int(task_cfg.get("impact_depth", 2)),
        "include_globs": " ".join(repo_cfg.get("include") or []),
        "exclude_globs": " ".join(repo_cfg.get("exclude") or []),
        "file_pattern": _include_basename(repo_cfg),
        "semantic_query": json.dumps([base_query]),
        # Private values used by Python runners; not rendered into argv.
        "_symbols": symbols,
        "_words": words,
        "_repo_cfg": repo_cfg,
        "_task_cfg": task_cfg,
        "_tool_cfg": tool_cfg,
    }


def render_argv(argv, ctx: dict) -> list[str]:
    return [render(token, ctx) for token in argv]


def prepare_tool(tool_cfg: dict, repo, ctx: dict, default_timeout: float = 900) -> float | None:
    argv = tool_cfg.get("prepare")
    if not argv:
        return None
    cwd = tool_cfg.get("cwd", "{repo}")
    timeout = float(tool_cfg.get("prepare_timeout", default_timeout))
    rc, _out, err, elapsed = run_cmd(
        render_argv(argv, ctx),
        cwd=render(cwd, ctx) if cwd else repo,
        timeout=timeout,
        env=tool_cfg.get("env"),
    )
    if rc != 0:
        raise RuntimeError(f"prepare failed (rc={rc}): {err.strip()[-600:]}")
    return round(elapsed, 3)


def cleanup_tool(tool_cfg: dict, repo, ctx: dict) -> None:
    argv = tool_cfg.get("cleanup")
    if not argv:
        return
    cwd = tool_cfg.get("cwd", "{repo}")
    try:
        run_cmd(
            render_argv(argv, ctx),
            cwd=render(cwd, ctx) if cwd else repo,
            timeout=float(tool_cfg.get("cleanup_timeout", 60)),
            env=tool_cfg.get("env"),
        )
    except Exception:
        pass


def tool_version(tool_cfg: dict) -> str:
    version = tool_cfg.get("version")
    if not version:
        return ""
    try:
        rc, out, err, _ = run_cmd(version, timeout=30)
        if rc == 0:
            return (out or err).strip().splitlines()[0][:200]
    except Exception:
        pass
    return "unknown"


def execute_tool(
    tool_name: str,
    tool_cfg: dict,
    task_name: str,
    task_cfg: dict,
    instance: dict,
    repo,
    repo_cfg: dict,
    args,
    prepare_s: float | None = None,
) -> dict:
    """Run one query for one (instance, tool) pair and return a partial row."""
    query_cfg = query_for_task(tool_cfg, task_name, task_cfg)
    if query_cfg is None:
        raise RuntimeError(f"tool '{tool_name}' is not configured for task '{task_name}'")
    ctx = build_context(instance, task_name, task_cfg, tool_cfg, args, repo, repo_cfg)
    record = {
        "tool": tool_name,
        "task": task_name,
        "prepare_s": prepare_s,
        "query_s": None,
        "query_s_samples": [],
        "results": [],
        "raw_items": 0,
        "parsed_items": 0,
        "out_of_scope": 0,
        "stdout_chars": 0,
        "empty_result": False,
    }

    repeat = max(1, int(getattr(args, "repeat", 1) or 1))
    scope_filter = bool(getattr(args, "scope_filter", True))
    if not bool(query_cfg.get("scope_filter", tool_cfg.get("scope_filter", True))):
        scope_filter = False

    runner_name = query_cfg.get("runner")
    samples: list[float] = []
    items: list[dict] = []
    for _ in range(repeat):
        if runner_name:
            runner = get_runner(runner_name) or resolve_plugin(runner_name)
            if runner is None:
                raise RuntimeError(f"unknown runner '{runner_name}'")
            items, elapsed = runner(
                query_cfg, repo, ctx, repo_cfg,
                float(query_cfg.get("timeout", 300)),
            )
            samples.append(float(elapsed))
        else:
            argv = query_cfg.get("argv")
            if not argv:
                raise RuntimeError(f"query for task '{task_name}' has no argv")
            cwd = query_cfg.get("cwd", "{repo}")
            rc, out, err, elapsed = run_cmd(
                render_argv(argv, ctx),
                cwd=render(cwd, ctx) if cwd else repo,
                timeout=float(query_cfg.get("timeout", 300)),
                env=query_cfg.get("env") or tool_cfg.get("env"),
            )
            samples.append(float(elapsed))
            record["stdout_chars"] += len(out or "")
            if rc != 0:
                raise RuntimeError(f"query failed (rc={rc}): {err.strip()[-600:]}")
            parser = query_cfg.get("parse", "recursive_json")
            if parser in PARSERS:
                items = parse_results(parser, out, repo, query_cfg)
            else:
                function = resolve_plugin(parser)
                if function is None:
                    raise RuntimeError(f"unknown parser '{parser}'")
                items = function(out, repo, query_cfg)

    record["query_s"] = round(statistics.median(samples), 3) if samples else None
    record["query_s_samples"] = [round(value, 3) for value in samples]

    # Normalise once more: custom runners/parsers may return absolute paths.
    normalised = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        path = normalize_path(item.get("path"), repo)
        if not path:
            continue
        if scope_filter and not path_in_scope(path, repo_cfg):
            record["out_of_scope"] += 1
            continue
        normalised.append({**item, "path": path})
    record["raw_items"] = len(items or [])
    record["parsed_items"] = len(normalised)
    record["empty_result"] = not normalised
    record["results"] = normalised[: max(int(getattr(args, "top_k", 10)), 1)]
    return record
