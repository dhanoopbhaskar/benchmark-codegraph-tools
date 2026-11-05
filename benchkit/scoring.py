"""Scoring and aggregation for benchmark tasks."""
from __future__ import annotations

import random
import re
import statistics
from pathlib import Path


def metric_k(metric: str, default: int = 10) -> int:
    match = re.search(r"@(\d+)", metric or "")
    return int(match.group(1)) if match else default


def overlaps(start, end, gold_start, gold_end) -> bool:
    if start is None:
        return False
    end = start if end is None else end
    try:
        start, end = int(start), int(end)
        gold_start = int(gold_start)
        gold_end = int(gold_end if gold_end is not None else gold_start)
    except (TypeError, ValueError):
        return False
    return start <= gold_end and gold_start <= end


def unique_paths(items, limit: int) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = item.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _valid_items(items, repo) -> int:
    root = Path(repo)
    count = 0
    for item in items:
        path = item.get("path")
        if not path:
            continue
        try:
            if (root / path).is_file():
                count += 1
        except OSError:
            pass
    return count


def _context_lines(items, repo, limit_paths: int | None = None) -> int:
    root = Path(repo)
    selected = items
    if limit_paths is not None:
        allowed = set(unique_paths(items, limit_paths))
        selected = [item for item in items if item.get("path") in allowed]
    total = 0
    for item in selected:
        path = item.get("path")
        if not path:
            continue
        try:
            if not (root / path).is_file():
                continue
        except OSError:
            continue
        start, end = item.get("line"), item.get("end_line")
        if start and end:
            try:
                total += max(0, int(end) - int(start) + 1)
            except (TypeError, ValueError):
                total += 1
        else:
            total += 1
    return total


def score_localization(items, instance, k: int, repo) -> dict:
    gold_files = set(instance.get("gold", {}).get("files") or [])
    gold_hunks = instance.get("gold", {}).get("hunks") or {}
    paths = unique_paths(items, k)

    rank = next((index for index, path in enumerate(paths, 1) if path in gold_files), None)
    top5 = set(unique_paths(items, 5))
    hunk_hit = False
    for item in items:
        if item.get("path") not in gold_files or item.get("path") not in top5:
            continue
        for start, end in gold_hunks.get(item["path"], []):
            if overlaps(item.get("line"), item.get("end_line"), start, end):
                hunk_hit = True
                break
        if hunk_hit:
            break

    return {
        "rank": rank,
        "mrr@10": (1.0 / rank) if rank and rank <= 10 else 0.0,
        "recall@1": bool(rank and rank <= 1),
        "recall@3": bool(rank and rank <= 3),
        "recall@5": bool(rank and rank <= 5),
        "recall@10": bool(rank and rank <= 10),
        "hunk@5": hunk_hit,
        "gold_found": 1 if rank else 0,
        "gold_total": 1,
        "files_returned": len(paths),
        "invalid_paths": len(items) - _valid_items(items, repo),
        "context_lines": _context_lines(items, repo),
    }


def score_site(items, instance, k: int, repo) -> dict:
    """Score a task whose gold is one or more file/line definition sites."""
    sites = instance.get("gold", {}).get("sites") or []
    paths = unique_paths(items, k)

    rank = None
    for index, item in enumerate(items, 1):
        for site in sites:
            if item.get("path") != site.get("path"):
                continue
            gold_line = site.get("line")
            if gold_line is None:
                rank = index
                break
            if overlaps(item.get("line"), item.get("end_line"),
                        gold_line, site.get("end_line")):
                rank = index
                break
        if rank is not None:
            break

    metrics = {
        "rank": rank,
        "mrr@10": (1.0 / rank) if rank and rank <= 10 else 0.0,
        "gold_found": 1 if rank else 0,
        "gold_total": len({(s.get("path"), s.get("line")) for s in sites}) or 1,
        "files_returned": len(paths),
        "invalid_paths": len(items) - _valid_items(items, repo),
        "context_lines": _context_lines(items, repo),
    }
    for key in ("hit@1", "hit@3", "hit@5", "hit@10"):
        limit = metric_k(key)
        metrics[key] = bool(rank and rank <= limit)
    return metrics


def score_set(items, instance, k: int, repo) -> dict:
    """Score file-level set recall (callers, callees, impact, dependents)."""
    gold = set(instance.get("gold", {}).get("files") or [])
    metrics = {
        "gold_total": len(gold),
        "files_returned": len(unique_paths(items, k)),
        "invalid_paths": len(items) - _valid_items(items, repo),
        "context_lines": _context_lines(items, repo),
    }
    first_rank = None
    for index, item in enumerate(items, 1):
        if item.get("path") in gold:
            first_rank = index
            break
    metrics["rank"] = first_rank
    metrics["mrr@10"] = (1.0 / first_rank) if first_rank and first_rank <= 10 else 0.0

    # Materialise the common cut-offs plus the run's top-k.
    for limit in sorted({1, 3, 5, 10, k}):
        top = unique_paths(items, limit)
        found = len([path for path in top if path in gold])
        metrics[f"recall@{limit}"] = (found / len(gold)) if gold else 0.0
        metrics[f"hit@{limit}"] = bool(found)
        metrics[f"all@{limit}"] = bool(gold) and found == len(gold)
        metrics[f"precision@{limit}"] = (found / len(top)) if top else 0.0
    metrics["gold_found"] = len(set(unique_paths(items, k)) & gold)
    return metrics


SCORERS = {
    "localization": score_localization,
    "site": score_site,
    "set": score_set,
}


def score_instance(scorer: str, items, instance, k: int, repo) -> dict:
    function = SCORERS.get(scorer)
    if function is None and ":" in str(scorer):
        from .plugins import resolve as resolve_plugin

        function = resolve_plugin(scorer)
    if function is None:
        raise SystemExit(f"unknown scorer '{scorer}'; known: {sorted(SCORERS)}")
    return function(items, instance, k, repo)


PERCENT_PREFIXES = ("recall@", "hit@", "all@", "precision@", "hunk@")


def metric_is_percent(metric: str) -> bool:
    return metric.startswith(PERCENT_PREFIXES)


def bootstrap_ci(values: list[float], n: int = 1000, alpha: float = 0.05, seed: int = 0):
    if len(values) < 3:
        return None, None
    rng = random.Random(seed)
    means = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(n)
    )
    return means[int((alpha / 2) * n)], means[min(n - 1, int((1 - alpha / 2) * n))]


def aggregate_rows(rows: list[dict], task_cfg: dict, top_k: int) -> dict:
    """Aggregate one tool's rows for one task."""
    ok = [row for row in rows if not row.get("error") and row.get("metrics")]
    metrics = [row["metrics"] for row in ok]
    configured = list(task_cfg.get("metrics") or [])
    values: dict[str, float | None] = {}
    for metric in configured:
        if metric.startswith(("recall@", "hit@", "all@", "precision@")) and metric_k(metric, top_k) > top_k:
            values[metric] = None
            continue
        # Percent metrics are either booleans (hit/all/localization recall) or
        # fractions (set recall/precision). ``float`` preserves both; coercing
        # truthiness would turn 0.33 recall into 1.0 and inflate the summary.
        samples = [
            float(item[metric])
            for item in metrics
            if item.get(metric) is not None
        ]
        if not samples:
            values[metric] = None
        elif metric_is_percent(metric):
            values[metric] = 100.0 * statistics.mean(samples)
        else:
            values[metric] = statistics.mean(samples)

    summary = {
        "instances": len(ok),
        "errors": len(rows) - len(ok),
        "metrics": values,
        "files_returned": statistics.median(
            [item["files_returned"] for item in metrics]
        ) if metrics else 0,
        "context_lines": statistics.median(
            [item["context_lines"] for item in metrics]
        ) if metrics else 0,
        "invalid_paths": sum(item.get("invalid_paths", 0) for item in metrics),
        "prepare_s": statistics.median(
            [row["prepare_s"] for row in ok if row.get("prepare_s") is not None]
        ) if any(row.get("prepare_s") is not None for row in ok) else 0.0,
        "query_s": statistics.median(
            [row["query_s"] for row in ok if row.get("query_s") is not None]
        ) if any(row.get("query_s") is not None for row in ok) else 0.0,
        "ranks": [item.get("rank") for item in metrics],
    }

    if configured:
        primary = configured[0]
        if primary.startswith(("recall@", "hit@", "all@", "precision@")) and metric_k(primary, top_k) > top_k:
            summary["primary"] = primary
            summary["primary_ci"] = [None, None]
            return summary
        samples = [
            float(item[primary])
            for item in metrics
            if item.get(primary) is not None
        ]
        lo, hi = bootstrap_ci(samples)
        scale = 100.0 if metric_is_percent(primary) else 1.0
        summary["primary"] = primary
        summary["primary_ci"] = [
            lo * scale if lo is not None else None,
            hi * scale if hi is not None else None,
        ]
    return summary
