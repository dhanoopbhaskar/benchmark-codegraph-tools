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
    for index, item in enumerate(_unique_items(items), 1):
        if any(_matches_site(item, site) for site in sites):
            rank = index
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
    for limit in (5, 10):
        metrics.update(_site_metrics(items, sites, limit))
    return metrics


def _item_key(item: dict) -> tuple:
    return (
        item.get("path"), item.get("line"), item.get("end_line"),
        item.get("symbol"),
    )


def _unique_items(items) -> list[dict]:
    seen: set[tuple] = set()
    output: list[dict] = []
    for item in items:
        key = _item_key(item)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _matches_site(item: dict, site: dict) -> bool:
    if item.get("path") != site.get("path"):
        return False
    gold_line = site.get("line")
    if gold_line is None:
        return True
    return overlaps(item.get("line"), item.get("end_line"), gold_line, site.get("end_line"))


def _site_metrics(items, gold_sites: list[dict], limit: int) -> dict:
    """Site-level recall/precision inside the top-*limit* unique files."""
    if not gold_sites:
        return {}
    top_paths = set(unique_paths(items, limit))
    candidates = [
        item for item in _unique_items(items)
        if item.get("path") in top_paths
    ]
    matched_sites = set()
    matched_items = 0
    for item in candidates:
        hit = False
        for site in gold_sites:
            if _matches_site(item, site):
                matched_sites.add(
                    (site.get("path"), site.get("line"), site.get("name"))
                )
                hit = True
        if hit:
            matched_items += 1
    total_sites = len({
        (site.get("path"), site.get("line"), site.get("name"))
        for site in gold_sites
    })
    return {
        f"site_recall@{limit}": (len(matched_sites) / total_sites) if total_sites else 0.0,
        f"site_precision@{limit}": (matched_items / len(candidates)) if candidates else 0.0,
        f"site_hit@{limit}": bool(matched_sites),
        f"all_sites@{limit}": bool(total_sites) and len(matched_sites) == total_sites,
        f"extra_sites@{limit}": max(0, len(candidates) - matched_items),
    }


def _edge_key(path, source, target):
    return (path or "", source or "", target or "")


def _returned_edges(items, task: str, target: str, limit: int) -> set[tuple]:
    """Map returned items onto gold ``(path, from, to)`` edge identities.

    A tool that only returns files (no symbol) cannot claim an edge; file-level
    metrics still score it normally.
    """
    output: set[tuple] = set()
    top_paths = set(unique_paths(items, limit))
    for item in _unique_items(items):
        if item.get("path") not in top_paths:
            continue
        symbol = item.get("symbol")
        if not symbol:
            continue
        if task in {"callees", "implementations", "overrides"}:
            output.add(_edge_key(item.get("path"), target, symbol))
        else:
            output.add(_edge_key(item.get("path"), symbol, target))
    return output


def _edge_metrics(items, instance: dict, task: str, limit: int) -> dict:
    if task == "impact":
        # Impact edges are transitive; a returned caller item does not identify
        # the immediate hop, so file/site metrics are the honest signal there.
        return {}
    gold_edges = instance.get("gold", {}).get("edges") or []
    if not gold_edges:
        return {}
    gold = {
        _edge_key(edge.get("path"), edge.get("from"), edge.get("to"))
        for edge in gold_edges
    }
    returned = _returned_edges(items, task, str(instance.get("query", {}).get("symbol") or ""), limit)
    found = len(gold & returned)
    precision = found / len(returned) if returned else 0.0
    recall = found / len(gold) if gold else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        f"edge_recall@{limit}": recall,
        f"edge_precision@{limit}": precision,
        f"edge_f1@{limit}": f1,
        f"edge_hit@{limit}": bool(found),
    }


def score_set(items, instance, k: int, repo) -> dict:
    """Score file-level set recall plus optional site/edge correctness."""
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

    gold_sites = instance.get("gold", {}).get("sites") or []
    task = instance.get("task", "")
    for limit in sorted({1, 3, 5, 10, k}):
        top = unique_paths(items, limit)
        found = len([path for path in top if path in gold])
        metrics[f"recall@{limit}"] = (found / len(gold)) if gold else 0.0
        metrics[f"hit@{limit}"] = bool(found)
        metrics[f"all@{limit}"] = bool(gold) and found == len(gold)
        metrics[f"precision@{limit}"] = (found / len(top)) if top else 0.0
    metrics.update(_site_metrics(items, gold_sites, 10))
    metrics.update(_site_metrics(items, gold_sites, 5))
    metrics.update(_edge_metrics(items, instance, task, 10))
    metrics["gold_found"] = len(set(unique_paths(items, k)) & gold)
    return metrics


def score_sites(items, instance, k: int, repo) -> dict:
    """Score a task whose gold is every declaration of an ambiguous name."""
    sites = instance.get("gold", {}).get("sites") or []
    paths = unique_paths(items, k)
    metrics = {
        "gold_total": len({
            (site.get("path"), site.get("line")) for site in sites
        }) or 1,
        "files_returned": len(paths),
        "invalid_paths": len(items) - _valid_items(items, repo),
        "context_lines": _context_lines(items, repo),
    }
    first_rank = None
    for index, item in enumerate(_unique_items(items), 1):
        if any(_matches_site(item, site) for site in sites):
            first_rank = index
            break
    metrics["rank"] = first_rank
    metrics["mrr@10"] = (1.0 / first_rank) if first_rank and first_rank <= 10 else 0.0
    metrics["gold_found"] = 0
    for limit in sorted({1, 3, 5, 10, k}):
        metrics[f"hit@{limit}"] = bool(first_rank and first_rank <= limit)
    for limit in (5, 10):
        metrics.update(_site_metrics(items, sites, limit))
    metrics["gold_found"] = int(
        round(metrics.get("site_recall@10", 0.0) * metrics["gold_total"])
    )
    return metrics


def score_absence(items, instance, k: int, repo) -> dict:
    """Score negative controls: the correct answer is an empty result set."""
    paths = unique_paths(items, k)
    metrics = {
        "gold_total": 0,
        "files_returned": len(paths),
        "invalid_paths": len(items) - _valid_items(items, repo),
        "context_lines": _context_lines(items, repo),
        "rank": None,
        "mrr@10": 0.0,
    }
    for limit in sorted({1, 3, 5, 10, k}):
        top = unique_paths(items, limit)
        metrics[f"abstain@{limit}"] = not top
        metrics[f"clean@{limit}"] = not top
        metrics[f"false_positives@{limit}"] = len(top)
        metrics[f"precision@{limit}"] = 0.0 if top else 1.0
    metrics["gold_found"] = 0
    return metrics


SCORERS = {
    "localization": score_localization,
    "site": score_site,
    "sites": score_sites,
    "set": score_set,
    "absence": score_absence,
}


def score_instance(scorer: str, items, instance, k: int, repo) -> dict:
    function = SCORERS.get(scorer)
    if function is None and ":" in str(scorer):
        from .plugins import resolve as resolve_plugin

        function = resolve_plugin(scorer)
    if function is None:
        raise SystemExit(f"unknown scorer '{scorer}'; known: {sorted(SCORERS)}")
    return function(items, instance, k, repo)


PERCENT_PREFIXES = (
    "recall@", "hit@", "all@", "precision@", "hunk@",
    "site_recall@", "site_precision@", "site_hit@", "all_sites@",
    "edge_recall@", "edge_precision@", "edge_f1@", "edge_hit@",
    "symbol_recall@", "symbol_precision@", "abstain@", "clean@",
)


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


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _is_k_metric(metric: str) -> bool:
    return metric.startswith((
        "recall@", "hit@", "all@", "precision@",
        "site_recall@", "site_precision@", "site_hit@", "all_sites@",
        "edge_recall@", "edge_precision@", "edge_f1@", "edge_hit@",
        "abstain@", "clean@", "false_positives@",
    ))


def aggregate_rows(rows: list[dict], task_cfg: dict, top_k: int) -> dict:
    """Aggregate one tool's rows for one task."""
    ok = [row for row in rows if not row.get("error") and row.get("metrics")]
    metrics = [row["metrics"] for row in ok]
    configured = list(task_cfg.get("metrics") or [])
    values: dict[str, float | None] = {}
    for metric in configured:
        if _is_k_metric(metric) and metric_k(metric, top_k) > top_k:
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

    prepare_samples = [
        row["prepare_s"] for row in ok if row.get("prepare_s") is not None
    ]
    query_samples = [
        value
        for row in ok
        for value in (row.get("query_s_samples") or [row.get("query_s")])
        if value is not None
    ]
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
        "out_of_scope": sum(row.get("out_of_scope", 0) for row in ok),
        "empty_results": sum(1 for row in ok if row.get("empty_result")),
        "prepare_s": statistics.median(prepare_samples) if prepare_samples else 0.0,
        "prepare_s_p95": _p95(prepare_samples),
        "query_s": statistics.median(query_samples) if query_samples else 0.0,
        "query_s_p95": _p95(query_samples),
        "query_samples": len(query_samples),
        "ranks": [item.get("rank") for item in metrics],
    }

    if configured:
        primary = configured[0]
        if _is_k_metric(primary) and metric_k(primary, top_k) > top_k:
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
