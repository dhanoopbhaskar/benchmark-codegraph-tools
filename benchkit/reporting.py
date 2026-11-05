"""Markdown / JSON / CSV reports from a run's results.jsonl."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from .config import BenchHome
from .scoring import aggregate_rows, metric_is_percent


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_results(run_dir):
    path = run_dir / "results.jsonl"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _fmt(metric: str, value) -> str:
    if value is None:
        return "n/a"
    if metric_is_percent(metric):
        return f"{float(value):.1f}"
    return f"{float(value):.2f}"


def _fmt_time(value) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}"


def _primary(metrics: dict):
    return next(iter(metrics), None)


def _write_task_section(lines, task_name, task_cfg, rows, tools_cfg, top_k, baseline):
    task_rows = [row for row in rows if row.get("task") == task_name]
    if not task_rows:
        return
    tools = sorted({row["tool"] for row in task_rows})
    metrics = list(task_cfg.get("metrics") or [])
    stats = {
        tool: aggregate_rows(
            [row for row in task_rows if row["tool"] == tool], task_cfg, top_k
        )
        for tool in tools
    }

    lines.append(f"## {task_name}")
    lines.append("")
    description = task_cfg.get("description")
    if description:
        lines.append(description)
        lines.append("")
    header = (
        "| Tool | Type | " + " | ".join(metrics)
        + " | Files | Lines | Prep s | Query s | Errors |"
    )
    lines.append(header)
    lines.append("|" + "---|" * (2 + len(metrics) + 5))
    for tool in tools:
        cfg = tools_cfg.get(tool, {})
        stat = stats[tool]
        ci_text = ""
        if stat.get("primary_ci") and stat["primary_ci"][0] is not None:
            ci_text = f" ({stat['primary_ci'][0]:.1f}–{stat['primary_ci'][1]:.1f})"
        cells = []
        for metric in metrics:
            value = stat["metrics"].get(metric)
            if metric == "hunk@5" and not cfg.get("has_lines", True):
                cells.append("—")
                continue
            text = _fmt(metric, value)
            if metric == stat.get("primary") and ci_text and value is not None:
                text += ci_text
            cells.append(text)
        lines.append(
            f"| {tool} | {cfg.get('kind', '')} | " + " | ".join(cells)
            + f" | {stat['files_returned']:.0f} | {stat['context_lines']:.0f}"
            + f" | {_fmt_time(stat['prepare_s'])} | {_fmt_time(stat['query_s'])}"
            + f" | {stat['errors']} |"
        )
    lines.append("")
    if metrics:
        primary = metrics[0]
        lines.append(
            f"Primary metric: **{primary}**; bracketed range is a seeded 95% "
            "bootstrap interval over instances. `Files`/`Lines` are medians per query."
        )
        lines.append("")

    repos = sorted({row["repo"] for row in task_rows})
    if len(repos) > 1:
        lines.append(f"### {task_name} — per repository")
        lines.append("")
        lines.append("| Tool | " + " | ".join(repos) + " |")
        lines.append("|---|" + "---|" * len(repos))
        for tool in tools:
            cells = []
            for repo in repos:
                subset = [
                    row for row in task_rows
                    if row["tool"] == tool and row["repo"] == repo
                ]
                stat = aggregate_rows(subset, task_cfg, top_k)
                value = stat["metrics"].get(primary) if metrics else None
                cells.append(_fmt(primary, value) if metrics else str(stat["instances"]))
            lines.append(f"| {tool} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append(f"### {task_name} — per instance")
    lines.append("")
    if task_cfg.get("scorer") == "set":
        lines.append("Cell = gold files found / gold files total. `—` = none, `ERR` = tool failure.")
    else:
        lines.append("Cell = rank of the first matching gold file/site. `—` = not in top-k, `ERR` = tool failure.")
    lines.append("")
    lines.append("| Instance | " + " | ".join(tools) + " |")
    lines.append("|---|" + "---|" * len(tools))
    index = {(row["instance_id"], row["tool"]): row for row in task_rows}
    for instance_id in sorted({row["instance_id"] for row in task_rows}):
        cells = []
        for tool in tools:
            row = index.get((instance_id, tool))
            if row is None:
                cells.append("·")
            elif row.get("error"):
                cells.append("ERR")
            elif task_cfg.get("scorer") == "set":
                metrics_row = row.get("metrics") or {}
                found = metrics_row.get("gold_found", 0)
                total = metrics_row.get("gold_total", 0)
                cells.append(f"{found}/{total}" if found else "—")
            else:
                rank = (row.get("metrics") or {}).get("rank")
                cells.append(str(rank) if rank else "—")
        lines.append(f"| {instance_id} | " + " | ".join(cells) + " |")
    lines.append("")

    if baseline and baseline in tools and len(tools) > 1:
        lines.append(f"### {task_name} — paired vs `{baseline}`")
        lines.append("")
        primary = metrics[0] if metrics else None
        lines.append("| Tool | Δ primary | Wins | Losses | Ties |")
        lines.append("|---|---:|---:|---:|---:|")
        index = {}
        for row in task_rows:
            if not row.get("error") and row.get("metrics"):
                index[(row["instance_id"], row["tool"])] = row["metrics"]
        baseline_map = {
            instance_id: value
            for (instance_id, tool), value in index.items()
            if tool == baseline
        }
        for tool in tools:
            if tool == baseline:
                continue
            diffs, wins, losses, ties = [], 0, 0, 0
            for (instance_id, name), value in index.items():
                if name != tool or instance_id not in baseline_map:
                    continue
                base_value = baseline_map[instance_id]
                if primary:
                    current = value.get(primary)
                    base = base_value.get(primary)
                    if current is not None and base is not None:
                        current_f = (1.0 if current else 0.0) if metric_is_percent(primary) else float(current)
                        base_f = (1.0 if base else 0.0) if metric_is_percent(primary) else float(base)
                        diffs.append(current_f - base_f)
                rank = value.get("rank") or 10**9
                base_rank = base_value.get("rank") or 10**9
                if rank < base_rank:
                    wins += 1
                elif rank > base_rank:
                    losses += 1
                else:
                    ties += 1
            if diffs:
                mean = sum(diffs) / len(diffs)
                if metric_is_percent(primary):
                    mean *= 100.0
                lines.append(f"| {tool} | {mean:+.1f} | {wins} | {losses} | {ties} |")
        lines.append("")

    errors = [row for row in task_rows if row.get("error")]
    if errors:
        lines.append(f"### {task_name} — tool errors")
        lines.append("")
        for row in errors:
            lines.append(f"- `{row['tool']}` on `{row['instance_id']}`: {row['error']}")
        lines.append("")

    by_instance: dict[str, list] = {}
    for row in task_rows:
        by_instance.setdefault(row["instance_id"], []).append(row)
    missed = []
    for instance_id, group in by_instance.items():
        successful = [row for row in group if not row.get("error") and row.get("metrics")]
        if not successful:
            continue
        if task_cfg.get("scorer") == "set":
            if all((row["metrics"].get("gold_found") or 0) == 0 for row in successful):
                missed.append(instance_id)
        elif all(row["metrics"].get("rank") is None for row in successful):
            missed.append(instance_id)
    if missed:
        lines.append(f"### {task_name} — instances nobody found")
        lines.append("")
        for instance_id in missed:
            row = next(row for row in task_rows if row["instance_id"] == instance_id)
            lines.append(f"- `{instance_id}` ({row['repo']})")
            for candidate in by_instance[instance_id]:
                top = ", ".join(candidate.get("top_paths", [])[:3]) or "—"
                lines.append(f"  - {candidate['tool']}: {top}")
        lines.append("")


def write_report(home: BenchHome, run_dir, baseline="rg", top_k=None):
    rows = load_results(run_dir)
    tasks_payload = home.load_tasks()
    tools_payload = home.load_tools()
    tasks_cfg = tasks_payload.get("tasks") or {}
    tools_cfg = tools_payload.get("tools") or {}
    run_meta = {}
    if (run_dir / "run.json").exists():
        run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    top_k = int(top_k or run_meta.get("top_k", 10))

    lines = [f"# Code graph benchmark — {run_dir.name}", ""]
    lines.append(f"Generated: {now_iso()}  ")
    lines.append(
        f"Instances: {len({row['instance_id'] for row in rows})}  |  "
        f"Tools: {len({row['tool'] for row in rows})}  |  top_k: {top_k}"
    )
    if run_meta:
        lines.append(
            f"Query: `{run_meta.get('query_mode', 'issue')}` mode, "
            f"{run_meta.get('max_query_chars', '?')} chars  |  "
            f"manifest `{str(run_meta.get('manifest_sha256') or '?')[:12]}`"
        )
    lines.append("")
    lines.append(
        "Tasks are independent; a tool only appears in a task it is configured "
        "for. Localization measures issue-driven code discovery. Structural tasks "
        "measure graph traversal against deterministic, regex-derived ground truth."
    )
    lines.append("")

    for task_name, task_cfg in tasks_cfg.items():
        if any(row.get("task") == task_name for row in rows):
            _write_task_section(
                lines, task_name, task_cfg, rows, tools_cfg, top_k, baseline
            )

    summary = {
        "run_id": run_dir.name,
        "generated_at": now_iso(),
        "top_k": top_k,
        "query_mode": run_meta.get("query_mode"),
        "max_query_chars": run_meta.get("max_query_chars"),
        "manifest_sha256": run_meta.get("manifest_sha256"),
        "baseline": baseline,
        "tasks": {},
    }
    for task_name, task_cfg in tasks_cfg.items():
        task_rows = [row for row in rows if row.get("task") == task_name]
        if not task_rows:
            continue
        tools = sorted({row["tool"] for row in task_rows})
        task_summary = {
            "instances": len({row["instance_id"] for row in task_rows}),
            "tools": {
                tool: aggregate_rows(
                    [row for row in task_rows if row["tool"] == tool],
                    task_cfg, top_k,
                )
                for tool in tools
            },
        }
        repos = sorted({row["repo"] for row in task_rows})
        if len(repos) > 1:
            task_summary["repos"] = {}
            for repo in repos:
                task_summary["repos"][repo] = {
                    tool: aggregate_rows(
                        [
                            row for row in task_rows
                            if row["tool"] == tool and row["repo"] == repo
                        ],
                        task_cfg, top_k,
                    )
                    for tool in tools
                }
        summary["tasks"][task_name] = task_summary

    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    _write_csv(run_dir, rows, tasks_cfg, top_k)
    print("\n".join(lines))


def _write_csv(run_dir, rows, tasks_cfg, top_k=10):
    summary_path = run_dir / "summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task", "repo", "tool", "metric", "value"])
        for task_name, task_cfg in tasks_cfg.items():
            task_rows = [row for row in rows if row.get("task") == task_name]
            if not task_rows:
                continue
            for tool in sorted({row["tool"] for row in task_rows}):
                for repo in sorted({row["repo"] for row in task_rows}) + [""]:
                    subset = [
                        row for row in task_rows
                        if row["tool"] == tool and (not repo or row["repo"] == repo)
                    ]
                    if not subset:
                        continue
                    stat = aggregate_rows(subset, task_cfg, top_k)
                    for metric, value in stat["metrics"].items():
                        writer.writerow([task_name, repo or "*", tool, metric, value])
                    writer.writerow([task_name, repo or "*", tool, "instances", stat["instances"]])
                    writer.writerow([task_name, repo or "*", tool, "errors", stat["errors"]])

    results_path = run_dir / "results.csv"
    metric_keys = sorted({
        key
        for row in rows
        for key in (row.get("metrics") or {}).keys()
    })
    with results_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "instance_id", "task", "repo", "tool", "error",
            "prepare_s", "query_s", "top_paths", *metric_keys,
        ])
        for row in rows:
            metrics = row.get("metrics") or {}
            writer.writerow([
                row.get("instance_id"), row.get("task"), row.get("repo"),
                row.get("tool"), row.get("error"), row.get("prepare_s"),
                row.get("query_s"), "|".join(row.get("top_paths") or []),
                *[metrics.get(key) for key in metric_keys],
            ])
