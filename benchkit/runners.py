"""Built-in Python runners for tools that are not a single query command."""
from __future__ import annotations

import fnmatch
import json
import re

from .parsers import parse_cbm, parse_cbm_semantic, parse_results
from .process import run_cmd
from .text import normalize_path, render


def _glob_args(repo_cfg: dict) -> list[str]:
    args: list[str] = []
    for pattern in repo_cfg.get("include") or []:
        args.extend(["--glob", pattern])
    for pattern in repo_cfg.get("exclude") or []:
        args.extend(["--glob", "!" + pattern])
    return args


def _rg_base(repo_cfg: dict) -> list[str]:
    return ["rg", "--no-messages", *_glob_args(repo_cfg)]


def rg_localization(cfg, repo, ctx, repo_cfg, timeout=300):
    """Rank files by weighted distinct issue identifiers (the keyword baseline)."""
    symbols = list(ctx.get("_symbols") or [])
    words = list(ctx.get("_words") or [])
    hits: dict[str, float] = {}
    elapsed = 0.0
    for weight, terms in ((3, symbols), (1, words)):
        for term in terms:
            rc, out, err, dt = run_cmd(
                [*_rg_base(repo_cfg), "-l", "-i", "-F", "--", term, "."],
                cwd=repo,
                timeout=min(60, timeout),
            )
            elapsed += dt
            if rc not in (0, 1):
                raise RuntimeError(f"rg failed: {err.strip()[:300]}")
            for line in out.splitlines():
                path = normalize_path(line, repo)
                if path:
                    hits[path] = hits.get(path, 0.0) + weight
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[: int(ctx.get("k", 10))]
    return [
        {"path": path, "line": None, "end_line": None, "score": score}
        for path, score in ranked
    ], round(elapsed, 3)


def rg_symbol(cfg, repo, ctx, repo_cfg, timeout=300):
    """Literal symbol matches as a file/line baseline for definition tasks."""
    symbol = str(ctx.get("symbol") or "").strip()
    if not symbol:
        return [], 0.0
    rc, out, err, dt = run_cmd(
        [*_rg_base(repo_cfg), "-n", "-w", "-F", "--", symbol, "."],
        cwd=repo,
        timeout=timeout,
    )
    if rc not in (0, 1):
        raise RuntimeError(f"rg failed: {err.strip()[:300]}")
    items = []
    for line in out.splitlines():
        match = re.match(r"^(.*?):(\d+):", line)
        if not match:
            continue
        items.append({
            "path": normalize_path(match.group(1), repo),
            "line": int(match.group(2)),
            "end_line": int(match.group(2)),
            "score": None,
            "symbol": symbol,
        })
    return items[: int(ctx.get("k", 10))], round(dt, 3)


def rg_callers(cfg, repo, ctx, repo_cfg, timeout=300):
    """Call-like textual occurrences of a symbol: a simple caller baseline."""
    symbol = str(ctx.get("symbol") or "").strip()
    if not symbol:
        return [], 0.0
    pattern = r"\b" + re.escape(symbol) + r"\s*\("
    rc, out, err, dt = run_cmd(
        [*_rg_base(repo_cfg), "-l", "-e", pattern, "."],
        cwd=repo,
        timeout=timeout,
    )
    if rc not in (0, 1):
        raise RuntimeError(f"rg failed: {err.strip()[:300]}")
    items = [
        {"path": normalize_path(line, repo), "line": None, "end_line": None, "score": None}
        for line in out.splitlines()
    ]
    return items[: int(ctx.get("k", 10))], round(dt, 3)


def _cbm_path_allowed(path: str, repo_cfg: dict) -> bool:
    """Apply the repository include/exclude globs to a tool-returned path."""
    includes = repo_cfg.get("include") or []
    excludes = repo_cfg.get("exclude") or []

    def matches(pattern: str) -> bool:
        pattern = str(pattern).replace("\\", "/")
        # fnmatch treats ``*`` as crossing ``/``, so ``**/src/test/**`` works.
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern)

    if includes and not any(matches(pattern) for pattern in includes):
        return False
    return not any(matches(pattern) for pattern in excludes)


def _unique_paths(items: list[dict], repo_cfg: dict) -> list[str]:
    paths: list[str] = []
    for item in items:
        path = item.get("path") or ""
        if path and path not in paths and _cbm_path_allowed(path, repo_cfg):
            paths.append(path)
    return paths


def cbm_fusion(cfg, repo, ctx, repo_cfg, timeout=300):
    """Fuse codebase-memory term search and semantic search with weighted RRF.

    ``search_code`` is a symbol/regex search, not a natural-language engine, so a
    single OR query over issue keywords is dominated by high-frequency files.
    Instead, search each extracted term separately and rank files by how many
    distinct terms they match, then fuse that list with ``search_graph
    --semantic-query``. Both lists are restricted to the repository's source
    globs so build/docs/test noise cannot crowd out code.
    """
    elapsed = 0.0
    k = int(ctx.get("k", 10))
    terms: list[str] = []
    for term in list(ctx.get("_symbols") or [])[:8] + list(ctx.get("_words") or [])[:4]:
        term = str(term or "").strip()
        if term and term not in terms:
            terms.append(term)
    if not terms:
        terms = [str(ctx.get("query") or "").strip()][:1]

    coverage: dict[str, int] = {}
    rrf: dict[str, float] = {}
    details: dict[str, dict] = {}
    for term in terms:
        term_ctx = dict(ctx)
        pattern = re.escape(term)
        term_ctx["keywords_regex"] = pattern
        term_ctx["term"] = pattern
        rc, out, err, dt = run_cmd(
            [render(token, term_ctx) for token in cfg["argv"]],
            cwd=repo,
            timeout=timeout,
        )
        elapsed += dt
        if rc != 0:
            continue
        paths: list[str] = []
        for item in parse_cbm(out, repo, cfg):
            path = item.get("path") or ""
            if not path or path in paths or not _cbm_path_allowed(path, repo_cfg):
                continue
            paths.append(path)
            details.setdefault(path, item)
        for rank, path in enumerate(paths, 1):
            coverage[path] = coverage.get(path, 0) + 1
            rrf[path] = rrf.get(path, 0.0) + 1.0 / (60 + rank)

    text_order = sorted(coverage, key=lambda path: (-coverage[path], -rrf[path], path))

    semantic_order: list[str] = []
    semantic_argv = cfg.get("semantic_argv")
    if semantic_argv:
        semantic_ctx = dict(ctx)
        semantic_ctx["semantic_query"] = json.dumps(
            [ctx.get("raw_query") or ctx.get("query") or ""]
        )
        rc, out, err, dt = run_cmd(
            [render(token, semantic_ctx) for token in semantic_argv],
            cwd=repo,
            timeout=timeout,
        )
        elapsed += dt
        if rc == 0:
            semantic_order = _unique_paths(parse_cbm_semantic(out, repo, cfg), repo_cfg)

    text_weight = min(max(float(cfg.get("text_weight", 0.6)), 0.0), 1.0)
    semantic_weight = 1.0 - text_weight
    scores: dict[str, float] = {}
    for rank, path in enumerate(text_order, 1):
        scores[path] = scores.get(path, 0.0) + text_weight / (60 + rank)
    for rank, path in enumerate(semantic_order, 1):
        scores[path] = scores.get(path, 0.0) + semantic_weight / (60 + rank)

    ranked = sorted(scores, key=lambda path: (-scores[path], path))[:k]
    items = []
    for path in ranked:
        item = dict(details.get(path) or {})
        item.update({
            "path": path,
            "line": item.get("line"),
            "end_line": item.get("end_line"),
            "score": scores[path],
            "symbol": item.get("symbol"),
        })
        items.append(item)
    return items, round(elapsed, 3)


RUNNERS = {
    "rg_localization": rg_localization,
    "rg_symbol": rg_symbol,
    "rg_callers": rg_callers,
    "cbm_fusion": cbm_fusion,
}


def get_runner(name: str):
    return RUNNERS.get(name)
