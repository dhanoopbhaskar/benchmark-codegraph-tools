"""Output parsers for tool adapters.

A parser returns a list of items::

    [{"path": "relative/path.java", "line": 10, "end_line": 20,
      "score": 0.9, "symbol": "Foo"}]

The generic ``recursive_json`` parser intentionally understands many shapes so
a new JSON-emitting CLI can be added without writing Python.
"""
from __future__ import annotations

import json
import re

from .text import extract_json, normalize_path

PATH_KEYS = (
    "filePath", "file_path", "filepath", "filename", "path", "file", "uri",
)
LINE_KEYS = (
    "startLine", "start_line", "line_start", "lineStart", "start",
    "line", "lines", "range", "span", "location",
)
END_LINE_KEYS = (
    "endLine", "end_line", "line_end", "lineEnd", "end",
)
SCORE_KEYS = ("score", "matches", "relevance", "rank_score")
SYMBOL_KEYS = ("symbol", "name", "qualifiedName", "qualified_name", "qn")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@/\\-]+\.[A-Za-z0-9]{1,8}(?::\d+(?::\d+)?)?")


def _coerce_range(value):
    """Return ``(start, end)`` from ints, ``'10-20'``, lists or ranges."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int):
        return value, value
    if isinstance(value, str):
        match = re.search(r"(\d+)(?:\s*[-:]\s*(\d+))?", value)
        if not match:
            return None, None
        start = int(match.group(1))
        return start, int(match.group(2) or start)
    if isinstance(value, (list, tuple)) and value:
        try:
            start = int(value[0])
            end = int(value[1]) if len(value) > 1 else start
            return start, end
        except (TypeError, ValueError):
            return None, None
    if isinstance(value, dict):
        for key in ("start", "from", "line", "start_line", "startLine"):
            if key in value:
                start, end = _coerce_range(value[key])
                if start is None:
                    continue
                for end_key in ("end", "to", "end_line", "endLine"):
                    if end_key in value:
                        _, resolved_end = _coerce_range(value[end_key])
                        return start, resolved_end or end
                return start, end
    return None, None


def _first(mapping: dict, keys) -> object:
    for key in keys:
        if key in mapping and mapping[key] not in (None, "", []):
            return mapping[key]
    return None


def _item_from_dict(value: dict, repo) -> dict | None:
    raw_path = _first(value, PATH_KEYS)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    raw_line = _first(value, LINE_KEYS)
    line, end_line = _coerce_range(raw_line)
    raw_end = _first(value, END_LINE_KEYS)
    if raw_end is not None:
        _, explicit_end = _coerce_range(raw_end)
        if explicit_end is not None:
            end_line = explicit_end
    score = _first(value, SCORE_KEYS)
    symbol = _first(value, SYMBOL_KEYS)
    if isinstance(symbol, dict):
        symbol = symbol.get("name") or symbol.get("qualifiedName")
    return {
        "path": normalize_path(raw_path, repo),
        "line": line,
        "end_line": end_line,
        "score": score if isinstance(score, (int, float)) else None,
        "symbol": symbol if isinstance(symbol, str) else None,
    }


def _walk_json(value, repo, output: list[dict], depth: int = 0) -> None:
    if depth > 30:
        return
    if isinstance(value, list):
        for element in value:
            _walk_json(element, repo, output, depth + 1)
        return
    if not isinstance(value, dict):
        return
    item = _item_from_dict(value, repo)
    if item and item["path"]:
        output.append(item)
    for key, child in value.items():
        # ``cols``/``rows`` are handled by the dedicated cbm parsers; walking
        # rows here would produce rows without path context.
        if key in {"cols", "rows", "columns"}:
            continue
        _walk_json(child, repo, output, depth + 1)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    output: list[dict] = []
    for item in items:
        if not item.get("path"):
            continue
        key = (item["path"], item.get("line"), item.get("end_line"), item.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def parse_recursive_json(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    data = extract_json(stdout)
    if data is None:
        return []
    output: list[dict] = []
    _walk_json(data, repo, output)
    return _dedupe(output)


def parse_semble(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    data = extract_json(stdout)
    if not isinstance(data, dict):
        return parse_recursive_json(stdout, repo, cfg)
    items = []
    for row in data.get("results", []) or []:
        if isinstance(row, dict):
            items.append({
                "path": normalize_path(row.get("file_path"), repo),
                "line": row.get("start_line"),
                "end_line": row.get("end_line"),
                "score": row.get("score"),
                "symbol": row.get("symbol"),
            })
    return _dedupe(items)


def parse_code_search(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    data = extract_json(stdout)
    if not isinstance(data, dict):
        return parse_recursive_json(stdout, repo, cfg)
    items = []
    for row in data.get("results", []) or []:
        if isinstance(row, dict):
            items.append({
                "path": normalize_path(row.get("file_path"), repo),
                "line": row.get("line_start"),
                "end_line": row.get("line_end"),
                "score": row.get("score"),
                "symbol": row.get("symbol"),
            })
    return _dedupe(items)


def _column_index(columns: list, predicate) -> int | None:
    for index, name in enumerate(columns):
        if predicate(str(name).lower()):
            return index
    return None


def _parse_table(columns: list, rows: list, repo) -> list[dict]:
    path_index = _column_index(
        columns,
        lambda name: name in {"file", "file_path", "path", "filename"}
        or name.endswith("file_path") or name.endswith(".path"),
    )
    if path_index is None:
        path_index = _column_index(columns, lambda name: "file" in name)
    lines_index = _column_index(
        columns,
        lambda name: name == "lines" or name.endswith("lines")
        or name in {"line", "start_line", "startline"}
        or name.endswith("start_line"),
    )
    name_index = _column_index(
        columns,
        lambda name: name in {"name", "symbol", "qn", "qualified_name"}
        or name.endswith(".name") or name.endswith("_name"),
    )
    score_index = _column_index(
        columns, lambda name: name in {"score", "matches", "relevance"}
    )
    items = []
    for row in rows:
        if path_index is None:
            continue
        if not isinstance(row, (list, tuple)) or path_index >= len(row):
            continue
        raw_path = row[path_index]
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        line = end_line = None
        if lines_index is not None and lines_index < len(row):
            line, end_line = _coerce_range(row[lines_index])
        items.append({
            "path": normalize_path(raw_path, repo),
            "line": line,
            "end_line": end_line,
            "score": row[score_index] if score_index is not None and score_index < len(row)
            and isinstance(row[score_index], (int, float)) else None,
            "symbol": row[name_index] if name_index is not None and name_index < len(row)
            and isinstance(row[name_index], str) else None,
        })
    return _dedupe(items)


def parse_cbm(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    data = extract_json(stdout)
    if data is None:
        return []
    if isinstance(data, dict):
        columns = data.get("cols") or data.get("columns")
        if columns and isinstance(data.get("rows"), list):
            return _parse_table(columns, data["rows"], repo)
        groups = data.get("groups")
        if isinstance(groups, list):
            items = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                raw_path = group.get("file") or group.get("file_path")
                if not isinstance(raw_path, str):
                    continue
                for row in group.get("rows") or []:
                    if not isinstance(row, (list, tuple)) or not row:
                        continue
                    line = end_line = None
                    if len(row) > 2:
                        line, end_line = _coerce_range(row[2])
                    items.append({
                        "path": normalize_path(raw_path, repo),
                        "line": line,
                        "end_line": end_line,
                        "score": None,
                        "symbol": row[0] if isinstance(row[0], str) else None,
                    })
            if items:
                return _dedupe(items)
    return parse_recursive_json(stdout, repo, cfg)


def parse_cbm_semantic(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    """Parse the ``semantic`` table returned by ``search_graph --semantic-query``."""
    data = extract_json(stdout)
    if isinstance(data, dict):
        semantic = data.get("semantic")
        if isinstance(semantic, dict):
            columns = semantic.get("cols") or semantic.get("columns")
            rows = semantic.get("rows")
            if columns and isinstance(rows, list):
                return _parse_table(columns, rows, repo)
    return []


def parse_cbm_query(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    data = extract_json(stdout)
    if not isinstance(data, dict):
        return parse_recursive_json(stdout, repo, cfg)
    columns = data.get("cols") or data.get("columns")
    rows = data.get("rows")
    if columns and isinstance(rows, list):
        return _parse_table(columns, rows, repo)
    return parse_recursive_json(stdout, repo, cfg)


def parse_lines(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    """Best-effort parser for plain-text output containing file paths."""
    items = []
    for raw_line in (stdout or "").splitlines():
        line = ANSI_RE.sub("", raw_line).strip()
        if not line or line.startswith(("#", ">", "-", "*", "ℹ", "⚠")):
            continue
        match = PATH_TOKEN_RE.search(line)
        if not match:
            continue
        token = match.group(0)
        start = end = None
        if ":" in token:
            token, _, line_part = token.partition(":")
            try:
                start = int(line_part.split(":")[0])
                end = start
            except ValueError:
                start = end = None
        items.append({
            "path": normalize_path(token, repo),
            "line": start,
            "end_line": end,
            "score": None,
            "symbol": None,
        })
    return _dedupe(items)


def _parse_codegraph_relation(stdout: str, repo, key: str) -> list[dict]:
    """Parse ``definitions[].callers/callees`` without the target definition.

    CodeGraph callers/callees responses also contain the target under
    ``targets`` and ``definitions[].definition``. A generic recursive parser
    turns that definition into a result, which both inflates recall and makes
    negative controls fail. Extract only the requested relation.
    """
    data = extract_json(stdout)
    if not isinstance(data, dict):
        return parse_recursive_json(stdout, repo)
    items = []
    for definition in data.get("definitions") or []:
        if not isinstance(definition, dict):
            continue
        for row in definition.get(key) or []:
            if not isinstance(row, dict):
                continue
            raw_path = row.get("filePath") or row.get("file_path") or row.get("path")
            if not isinstance(raw_path, str):
                continue
            line = row.get("startLine") or row.get("start_line")
            end_line = row.get("endLine") or row.get("end_line")
            items.append({
                "path": normalize_path(raw_path, repo),
                "line": line,
                "end_line": end_line if end_line is not None else line,
                "score": None,
                "symbol": row.get("name") or row.get("symbol"),
            })
    return _dedupe(items)


def parse_codegraph_callers(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    return _parse_codegraph_relation(stdout, repo, "callers")


def parse_codegraph_callees(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    return _parse_codegraph_relation(stdout, repo, "callees")


def parse_codegraph_affected(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    """Parse CodeGraph impact ``definitions[].affected``, excluding roots."""
    data = extract_json(stdout)
    if not isinstance(data, dict):
        return parse_recursive_json(stdout, repo)
    items = []
    for definition in data.get("definitions") or []:
        if not isinstance(definition, dict):
            continue
        roots = set(definition.get("roots") or [])
        for row in definition.get("affected") or []:
            if not isinstance(row, dict):
                continue
            if row.get("id") in roots:
                continue
            raw_path = row.get("filePath") or row.get("file_path") or row.get("path")
            if not isinstance(raw_path, str):
                continue
            line = row.get("startLine") or row.get("start_line")
            items.append({
                "path": normalize_path(raw_path, repo),
                "line": line,
                "end_line": row.get("endLine") or row.get("end_line") or line,
                "score": None,
                "symbol": row.get("name") or row.get("symbol"),
            })
    return _dedupe(items)


def parse_codegraph_dependents(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    """Parse ``codegraph node --symbols-only`` file mode.

    The dependents are only on the summary header, e.g.::

        **src/Owner.java** — 24 symbols, used by 2 files: src/A.java, src/B.java

    ``parse_lines`` skips that line because it starts with ``**``, which made
    the dependents task look like a tool failure. Extract the comma-separated
    list after ``used by N file(s):`` instead.
    """
    items = []
    pattern = re.compile(r"used by \d+ files?:\s*(.+)$", re.I)
    for raw_line in (stdout or "").splitlines():
        line = ANSI_RE.sub("", raw_line).strip()
        match = pattern.search(line)
        if not match:
            continue
        for token in match.group(1).split(","):
            token = token.strip().strip("`'\"")
            if not token:
                continue
            file_match = PATH_TOKEN_RE.search(token)
            if not file_match:
                continue
            items.append({
                "path": normalize_path(file_match.group(0), repo),
                "line": None,
                "end_line": None,
                "score": None,
                "symbol": None,
            })
    return _dedupe(items)


def parse_jsonl(stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    items = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        _walk_json(value, repo, items)
    return _dedupe(items)


PARSERS = {
    "recursive_json": parse_recursive_json,
    "generic_json": parse_recursive_json,
    "semble": parse_semble,
    "semble_graph": parse_semble,
    "code_search": parse_code_search,
    "codegraph": parse_recursive_json,
    "cbm": parse_cbm,
    "cbm_query": parse_cbm_query,
    "cbm_semantic": parse_cbm_semantic,
    "codegraph_callers": parse_codegraph_callers,
    "codegraph_callees": parse_codegraph_callees,
    "codegraph_affected": parse_codegraph_affected,
    "codegraph_dependents": parse_codegraph_dependents,
    "lines": parse_lines,
    "text_paths": parse_lines,
    "jsonl": parse_jsonl,
    "rg": parse_lines,
}


def parse_results(parser: str, stdout: str, repo, cfg: dict | None = None) -> list[dict]:
    function = PARSERS.get(parser or "recursive_json")
    if function is None:
        raise SystemExit(f"unknown parser '{parser}'; known: {sorted(PARSERS)}")
    return function(stdout, repo, cfg)
