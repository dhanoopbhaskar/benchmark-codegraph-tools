"""Mine benchmark instances from local git history.

Two families of tasks are supported out of the box:

``localization``
    issue/PR text -> production files and hunks touched by the fix commit.

``structural``
    ``symbol_definition`` / ``callers`` / ``callees`` / ``impact`` use a
    heuristic symbol index of the tree at ``base_commit`` to derive ground truth;
    ``dependents`` uses import edges. Ground truth is deterministic but
    regex-based, so reports label these tasks as heuristic.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil
from .config import BenchHome, load_json
from .languages import (
    EXCLUDED_SYMBOL_NAMES,
    RepoIndex,
    Symbol,
    detect_language,
    glob_match,
)
from .process import run_cmd
from .text import (
    clean_text,
    is_test_path,
    parse_hunks,
    parse_numstat,
    parse_refs,
)


# ---------------------------------------------------------------------------
# GitHub issue metadata (cached)
# ---------------------------------------------------------------------------

def gh_issue(home: BenchHome, slug: str, number: int, timeout: float = 60):
    cache = home.cache_dir / "gh" / f"{slug.replace('/', '__')}__{number}.json"
    if cache.exists():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            return payload.get("data") if payload.get("ok") else None
        except json.JSONDecodeError:
            pass
    rc, out, err, _ = 1, "", "", 0.0
    try:
        rc, out, err, _ = run_cmd(
            ["gh", "api", f"repos/{slug}/issues/{number}"], timeout=timeout
        )
    except Exception as exc:
        err = str(exc)
    data = None
    if rc == 0:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
    cache.parent.mkdir(parents=True, exist_ok=True)
    if data is not None or "Not Found" in (err or ""):
        cache.write_text(
            json.dumps({"ok": data is not None, "data": data}), encoding="utf-8"
        )
    if data is None and "Not Found" not in (err or "") and rc != 0:
        print(f"    ! gh api {slug}#{number}: {err.strip()[:140]}")
    return data


def problem_from_github(home: BenchHome, slug: str, refs):
    candidates = []
    seen = set()
    for owner, name, number in refs:
        ref_slug = f"{owner}/{name}" if owner else slug
        if (ref_slug, number) in seen:
            continue
        seen.add((ref_slug, number))
        data = gh_issue(home, ref_slug, number)
        if not data:
            continue
        title = clean_text(data.get("title"))
        body = clean_text(data.get("body"))
        is_pr = bool(data.get("pull_request"))

        if is_pr and len(body) < 400:
            linked = re.search(
                r"(?:backport of|fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)",
                body,
                re.I,
            )
            if linked:
                other = gh_issue(home, ref_slug, int(linked.group(1)))
                if other:
                    other_body = clean_text(other.get("body"))
                    if len(other_body) > len(body):
                        title = clean_text(other.get("title")) or title
                        body = other_body

        text = (title + "\n\n" + body).strip()
        if len(text) < 80:
            continue
        score = len(text) + (2500 if not is_pr else 0)
        candidates.append((score, text, data.get("html_url"), is_pr))

    if not candidates:
        return None
    _, text, url, is_pr = max(candidates, key=lambda item: item[0])
    return {"text": text, "url": url, "is_pr": is_pr}


# ---------------------------------------------------------------------------
# task helpers
# ---------------------------------------------------------------------------

def is_localization_task(task_cfg: dict) -> bool:
    return task_cfg.get("miner", "localization") in {"localization", "git_issue"}


def is_structural_task(task_cfg: dict) -> bool:
    return task_cfg.get("miner") == "structural"


def _setting(args, mining_cfg: dict, key: str, default):
    value = getattr(args, key, None)
    if value is not None:
        return value
    return mining_cfg.get(key, default)


def _slug(value: str, limit: int = 48) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-").lower()
    return value[:limit] or "item"


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _included_files(repo_cfg: dict, changed: list[tuple[str, int]]) -> list[tuple[str, int]]:
    included = []
    for path, lines in changed:
        if not any(glob_match(path, pattern) for pattern in repo_cfg.get("include") or ["**/*"]):
            continue
        if any(glob_match(path, pattern) for pattern in repo_cfg.get("exclude") or []):
            continue
        if is_test_path(path, repo_cfg.get("test_markers")):
            continue
        included.append((path, lines))
    return included


def _symbol_overlap(symbol: Symbol, hunks: list[list[int]]) -> bool:
    for start, end in hunks:
        if symbol.line <= end and start <= symbol.end_line:
            return True
    return False


def _candidate_symbols(index: RepoIndex, gold_files: list[str], gold_hunks: dict) -> list[Symbol]:
    candidates: dict[str, Symbol] = {}
    for path in gold_files:
        hunks = gold_hunks.get(path) or []
        symbols = index.symbols_in_file(path)
        overlapping = [symbol for symbol in symbols if _symbol_overlap(symbol, hunks)]
        selected = overlapping or [symbol for symbol in symbols if symbol.is_callable or symbol.is_type]
        for symbol in selected:
            candidates.setdefault(symbol.name, symbol)
    return list(candidates.values())


def _symbol_priority(symbol: Symbol) -> tuple:
    order = {
        "method": 0, "function": 0, "constructor": 1,
        "class": 2, "interface": 2, "enum": 2, "record": 2, "struct": 2,
    }
    return (order.get(symbol.kind, 5), symbol.line)


def _task_template(task_cfg: dict, task_name: str, symbol: Symbol | None, path: str) -> str:
    template = task_cfg.get("query_template")
    if not template:
        template = {
            "symbol_definition": "Where is `{symbol}` defined?",
            "callers": "Find all callers of `{symbol}`.",
            "callees": "Find all functions called by `{symbol}`.",
            "impact": "What code is affected if `{symbol}` changes?",
            "dependents": "Which files import `{path}`?",
        }.get(task_name, "Find `{symbol}`")
    return (
        str(template)
        .replace("{symbol}", symbol.name if symbol else "")
        .replace("{kind}", symbol.kind if symbol else "")
        .replace("{path}", path)
        .replace("{file}", path)
    )


def _site(symbol: Symbol) -> dict:
    return {
        "path": symbol.path,
        "line": symbol.line,
        "end_line": symbol.end_line,
        "name": symbol.name,
        "kind": symbol.kind,
    }


def _base_instance(commit_info: dict, repo_name: str, task_name: str) -> dict:
    return {
        "instance_id": "",
        "task": task_name,
        "repo": repo_name,
        "base_commit": commit_info["base"],
        "fix_commit": commit_info["sha"],
        "fix_date": commit_info["date"],
        "issue_ref": commit_info.get("issue_ref"),
        "title": "",
        "problem_statement": commit_info.get("problem_text", ""),
        "source_url": commit_info.get("source_url"),
        "gold": {"files": [], "hunks": {}, "sites": []},
        "query": {},
        "meta": {},
        "flags": {},
        "stats": {},
    }


def build_structural_instances(
    task_name: str,
    task_cfg: dict,
    index: RepoIndex,
    commit_info: dict,
    repo_name: str,
    gold_files: list[str],
    gold_hunks: dict,
    *,
    symbols_per_commit: int,
    used_ids: set[str],
) -> list[dict]:
    """Derive up to ``symbols_per_commit`` instances for one structural task."""
    exclude_names = set(EXCLUDED_SYMBOL_NAMES) | set(task_cfg.get("exclude_symbols") or [])
    max_gold = int(task_cfg.get("max_gold_files", 30))
    min_gold = int(task_cfg.get("min_gold_files", 1))
    instances: list[dict] = []

    if task_name == "dependents":
        for path in gold_files:
            importers = [p for p in index.importers_of(path) if index.should_include(p)]
            if not min_gold <= len(importers) <= max_gold:
                continue
            instance = _base_instance(commit_info, repo_name, task_name)
            query = _task_template(task_cfg, task_name, None, path)
            instance.update({
                "instance_id": _unique_id(
                    f"{repo_name}-{task_name}-{commit_info['short']}-{_slug(Path(path).stem)}",
                    used_ids,
                ),
                "title": query,
                "gold": {"files": sorted(importers), "hunks": {}, "sites": []},
                "query": {
                    "text": query,
                    "title": query,
                    "issue_text": commit_info.get("problem_text", ""),
                    "path": path,
                    "file": path,
                    "language": detect_language(path),
                },
                "meta": {
                    "path": path,
                    "gold_count": len(importers),
                    "heuristic_ground_truth": True,
                },
            })
            instance["stats"] = {
                "gold_files": len(importers),
                "gold_sites": 0,
                "problem_chars": len(commit_info.get("problem_text", "")),
            }
            instances.append(instance)
            if len(instances) >= symbols_per_commit:
                break
        return instances

    candidates = []
    for symbol in _candidate_symbols(index, gold_files, gold_hunks):
        if symbol.name in exclude_names or len(symbol.name) < 3:
            continue
        if task_name in {"symbol_definition", "callers", "callees", "impact"}:
            if index.unique_symbol(symbol.name) is None:
                continue
        candidates.append(symbol)
    candidates.sort(key=_symbol_priority)

    for symbol in candidates:
        gold_sites = []
        if task_name == "symbol_definition":
            gold_sites = [_site(symbol)]
            gold = sorted({symbol.path})
        elif task_name == "callers":
            callers = index.callers(symbol)
            if not callers:
                continue
            gold = sorted({caller.path for caller in callers})
            gold_sites = [_site(caller) for caller in callers]
        elif task_name == "callees":
            callees = index.callees(symbol)
            if not callees:
                continue
            gold = sorted({callee.path for callee in callees})
            gold_sites = [_site(callee) for callee in callees]
        elif task_name == "impact":
            depth = int(task_cfg.get("impact_depth", 2))
            affected = index.impact(symbol, depth)
            if not affected:
                continue
            gold = sorted({item.path for item in affected})
            gold_sites = [_site(item) for item in affected]
        else:
            continue

        if not min_gold <= len(gold) <= max_gold:
            continue
        query = _task_template(task_cfg, task_name, symbol, symbol.path)
        instance = _base_instance(commit_info, repo_name, task_name)
        instance.update({
            "instance_id": _unique_id(
                f"{repo_name}-{task_name}-{commit_info['short']}-{_slug(symbol.name)}",
                used_ids,
            ),
            "title": query,
            "gold": {"files": gold, "hunks": {}, "sites": gold_sites},
            "query": {
                "text": query,
                "title": query,
                "issue_text": commit_info.get("problem_text", ""),
                "symbol": symbol.name,
                "symbol_kind": symbol.kind,
                "path": symbol.path,
                "file": symbol.path,
                "language": symbol.language,
            },
            "meta": {
                "symbol": symbol.name,
                "symbol_kind": symbol.kind,
                "symbol_path": symbol.path,
                "symbol_line": symbol.line,
                "symbol_end_line": symbol.end_line,
                "gold_count": len(gold),
                "heuristic_ground_truth": True,
            },
        })
        instance["stats"] = {
            "gold_files": len(gold),
            "gold_sites": len(gold_sites),
            "problem_chars": len(commit_info.get("problem_text", "")),
        }
        instances.append(instance)
        if len(instances) >= symbols_per_commit:
            break
    return instances


def build_localization_instance(
    commit_info: dict,
    repo_name: str,
    gold_files: list[str],
    gold_hunks: dict,
    total_lines: int,
    used_ids: set[str],
    problem: dict,
) -> dict | None:
    text = problem["text"]
    gold_set = set(gold_files)
    instance = _base_instance(commit_info, repo_name, "localization")
    instance.update({
        "instance_id": _unique_id(f"{repo_name}-loc-{commit_info['short']}", used_ids),
        "title": text.split("\n", 1)[0][:200],
        "problem_statement": text,
        "source_url": problem.get("url"),
        "gold": {
            "files": gold_files,
            "hunks": {key: value for key, value in gold_hunks.items() if key in gold_set},
            "sites": [],
        },
        "query": {
            "text": text,
            "title": text.split("\n", 1)[0][:200],
            "issue_text": text,
        },
    })
    instance["meta"] = {
        "is_pr_text": problem.get("is_pr"),
        "gold_count": len(gold_files),
        "changed_lines": total_lines,
    }
    instance["flags"] = {
        "mentions_gold_path": any(
            Path(path).name.lower() in text.lower() for path in gold_files
        )
    }
    instance["stats"] = {
        "gold_files": len(gold_files),
        "gold_sites": 0,
        "changed_lines": total_lines,
        "problem_chars": len(text),
    }
    return instance


# ---------------------------------------------------------------------------
# repo mining
# ---------------------------------------------------------------------------

def mine_repo(
    home: BenchHome,
    repo_name: str,
    repo_cfg: dict,
    tasks: dict[str, dict],
    args,
    mining_cfg: dict | None = None,
) -> list[dict]:
    mining_cfg = mining_cfg or {}
    repo_path = home.repo_path(repo_cfg)
    if not gitutil.is_git_repo(repo_path):
        raise SystemExit(f"not a git repository: {repo_path}")
    slug = repo_cfg.get("slug") or gitutil.repo_slug(repo_path)
    if not slug:
        print(f"  ! could not derive a GitHub slug for {repo_name}; using commit messages")
    since = _setting(args, mining_cfg, "since", "2022-01-01")
    scan_limit = int(_setting(args, mining_cfg, "scan", 400))
    max_files = int(_setting(args, mining_cfg, "max_files", 6))
    max_lines = int(_setting(args, mining_cfg, "max_lines", 400))
    min_lines = int(_setting(args, mining_cfg, "min_lines", 2))
    min_problem = int(_setting(args, mining_cfg, "min_problem_chars", 200))
    max_problem = int(_setting(args, mining_cfg, "max_problem_chars", 8000))
    exclude_subject = _setting(args, mining_cfg, "exclude_subject", "")
    symbols_per_commit = int(
        getattr(args, "symbols_per_commit", None)
        or mining_cfg.get("symbols_per_commit", 1)
    )

    localization_tasks = [name for name, cfg in tasks.items() if is_localization_task(cfg)]
    structural_tasks = [name for name, cfg in tasks.items() if is_structural_task(cfg)]
    if not localization_tasks and not structural_tasks:
        raise SystemExit("no mineable tasks selected")

    limits = {name: int(args.limit) for name in tasks}
    found: dict[str, list[dict]] = defaultdict(list)
    used_ids: set[str] = set()
    problems_seen: set[str] = set()
    records = gitutil.log_records(repo_path, since)
    scanned = 0
    worktree = home.work_dir / "mine" / f"{repo_name}-{_slug(slug)}"

    print(
        f"mining {repo_name}: {len(records)} commits since {since}; "
        f"tasks={','.join(tasks)} limit={args.limit}/task"
    )

    for sha, timestamp, subject, body in records:
        if all(len(found[name]) >= limits[name] for name in tasks):
            break
        refs = parse_refs(subject)
        if exclude_subject and re.search(exclude_subject, subject):
            continue
        if not refs and not structural_tasks:
            continue
        scanned += 1
        if scanned > scan_limit:
            break

        base = gitutil.git(repo_path, "rev-parse", f"{sha}^", check=False).strip()
        if not base:
            continue

        changed = _included_files(
            repo_cfg, parse_numstat(gitutil.diff_numstat(repo_path, base, sha))
        )
        changed = [(path, lines) for path, lines in changed if lines > 0]
        if not changed:
            continue
        if len(changed) > max_files:
            print(f"  skip {sha[:8]} files={len(changed)} > {max_files}")
            continue
        total_lines = sum(lines for _, lines in changed)
        if total_lines > max_lines:
            print(f"  skip {sha[:8]} lines={total_lines} > {max_lines}")
            continue
        if total_lines < min_lines:
            print(f"  skip {sha[:8]} lines={total_lines} < {min_lines}")
            continue

        existing = [
            path for path, _ in changed
            if gitutil.file_exists_at(repo_path, base, path)
        ]
        changed = [(path, lines) for path, lines in changed if path in set(existing)]
        if not changed:
            continue

        diff = gitutil.diff_hunks(repo_path, base, sha, [p for p, _ in changed])
        hunks = parse_hunks(diff)
        if not hunks:
            continue

        commit_info = {
            "sha": sha,
            "short": sha[:8],
            "base": base,
            "date": datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d"),
            "issue_ref": f"#{refs[0][2]}" if refs else None,
            "subject": clean_text(subject),
            "body": clean_text(body),
            "problem_text": clean_text(subject + "\n\n" + body),
            "source_url": None,
        }

        # -- localization -------------------------------------------------
        if localization_tasks and any(
            len(found[name]) < limits[name] for name in localization_tasks
        ) and refs:
            problem = None
            if not getattr(args, "offline", False) and slug:
                problem = problem_from_github(home, slug, refs)
            if problem is None:
                fallback = clean_text(subject + "\n\n" + body)
                if len(fallback) >= min_problem:
                    problem = {"text": fallback, "url": None, "is_pr": None}
            if problem and len(problem["text"]) >= min_problem:
                text = problem["text"][:max_problem]
                digest = hashlib.sha1(
                    re.sub(r"\s+", " ", text.lower()).encode("utf-8")
                ).hexdigest()
                if digest not in problems_seen:
                    problems_seen.add(digest)
                    target_tasks = [
                        name for name in localization_tasks
                        if len(found[name]) < limits[name]
                    ]
                    for name in target_tasks:
                        commit_info["source_url"] = problem.get("url")
                        commit_info["problem_text"] = text
                        instance = build_localization_instance(
                            commit_info, repo_name,
                            [p for p, _ in changed], hunks, total_lines,
                            used_ids, {**problem, "text": text},
                        )
                        if instance is not None:
                            found[name].append(instance)
                            print(
                                f"  + {instance['instance_id']} [{name}] "
                                f"files={instance['stats']['gold_files']} "
                                f"lines={total_lines} chars={len(text)}"
                            )
                        commit_info["problem_text"] = clean_text(subject + "\n\n" + body)

        # -- structural ---------------------------------------------------
        pending = [
            name for name in structural_tasks if len(found[name]) < limits[name]
        ]
        if not pending:
            continue
        gold_files = [p for p, _ in changed]
        gold_hunks = {key: value for key, value in hunks.items() if key in set(gold_files)}
        try:
            gitutil.make_worktree(repo_path, base, worktree)
            index = RepoIndex(worktree, repo_cfg)
        except Exception as exc:
            print(f"  ! index build failed for {sha[:8]}: {exc}")
            gitutil.remove_worktree(repo_path, worktree)
            continue
        try:
            for name in pending:
                remaining = limits[name] - len(found[name])
                per_commit = min(
                    remaining,
                    int(tasks[name].get("symbols_per_commit", symbols_per_commit)),
                )
                try:
                    instances = build_structural_instances(
                        name, tasks[name], index, commit_info, repo_name,
                        gold_files, gold_hunks,
                        symbols_per_commit=per_commit,
                        used_ids=used_ids,
                    )
                except Exception as exc:
                    print(f"  ! {name} mining failed for {sha[:8]}: {exc}")
                    continue
                for instance in instances:
                    found[name].append(instance)
                    print(
                        f"  + {instance['instance_id']} [{name}] "
                        f"gold={instance['stats']['gold_files']}"
                    )
        finally:
            gitutil.remove_worktree(repo_path, worktree)

    return [instance for name in tasks for instance in found[name]]


def merge_manifest(
    existing: dict,
    new_instances: list[dict],
    repo_name: str,
    task_names: list[str],
    workspace: str,
) -> dict:
    keep = [
        instance for instance in existing.get("instances", [])
        if not (
            instance.get("repo") == repo_name
            and instance.get("task") in set(task_names)
        )
    ]
    merged = keep + new_instances
    merged.sort(
        key=lambda instance: (
            instance.get("repo", ""),
            instance.get("task", ""),
            instance.get("fix_date", ""),
            instance.get("instance_id", ""),
        ),
        reverse=True,
    )
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": workspace,
        "ground_truth": {
            "localization": "files and hunks changed by the fix commit",
            "structural": "regex-derived symbol/call/import graph at base_commit",
        },
        "instances": merged,
    }


def load_imported_instances(path) -> list[dict]:
    payload = load_json(path, None)
    if payload is None:
        raise SystemExit(f"no such import file: {path}")
    if isinstance(payload, list):
        instances = payload
    else:
        instances = payload.get("instances") or []
    output = []
    seen: set[str] = set()
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        instance.setdefault("task", "localization")
        instance.setdefault("gold", {})
        instance.setdefault("query", {})
        instance.setdefault("meta", {})
        instance.setdefault("flags", {})
        instance.setdefault("stats", {})
        if not instance.get("instance_id"):
            digest = hashlib.sha1(
                json.dumps(instance, sort_keys=True).encode("utf-8")
            ).hexdigest()[:10]
            instance["instance_id"] = f"import-{digest}"
        if instance["instance_id"] in seen:
            continue
        seen.add(instance["instance_id"])
        output.append(instance)
    return output
