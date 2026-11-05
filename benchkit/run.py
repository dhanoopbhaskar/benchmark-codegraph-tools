"""Run selected tools over selected instances and write result rows."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil
from .adapters import (
    build_context,
    cleanup_tool,
    execute_tool,
    normalize_tool,
    prepare_tool,
    query_for_task,
    tool_version,
)
from .config import BenchHome, select_config
from .mining import is_localization_task
from .process import which
from .scoring import score_instance, unique_paths

_write_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool_binary(tool_cfg: dict) -> str:
    from .process import binary_for

    return binary_for(tool_cfg, "prepare", "query")


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_rows(path: Path, rows: list[dict]) -> None:
    with _write_lock:
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _select_instances(manifest: dict, args, tasks: dict, repos: dict) -> list[dict]:
    instances = manifest.get("instances", [])
    task_names = {name.strip() for name in args.tasks.split(",") if name.strip()} if getattr(args, "tasks", None) else None
    repo_names = set(args.repo) if getattr(args, "repo", None) else None
    instance_ids = set(args.instance) if getattr(args, "instance", None) else None
    output = []
    for instance in instances:
        if task_names and instance.get("task") not in task_names:
            continue
        if repo_names and instance.get("repo") not in repo_names:
            continue
        if instance_ids and instance.get("instance_id") not in instance_ids:
            continue
        if instance.get("task") not in tasks:
            continue
        if instance.get("repo") not in repos:
            continue
        output.append(instance)
    if args.limit:
        output = output[: args.limit]
    return output


def cmd_run(home: BenchHome, args) -> Path:
    manifest_override = getattr(args, "manifest", None)
    if manifest_override:
        home.manifest_path = Path(manifest_override).expanduser().resolve()
    manifest = home.load_manifest()
    tools_payload = home.load_tools()
    tasks_payload = home.load_tasks()
    repos_payload = home.load_repos()

    tool_defaults = tools_payload.get("defaults") or {}
    all_tools = tools_payload.get("tools") or {}
    all_tasks = tasks_payload.get("tasks") or {}
    all_repos = repos_payload.get("repos") or {}

    tool_names = [name.strip() for name in args.tools.split(",") if name.strip()] if args.tools else None
    task_names = [name.strip() for name in args.tasks.split(",") if name.strip()] if args.tasks else None
    repo_names = args.repo

    tools = select_config(all_tools, tool_names, include_disabled=bool(tool_names))
    tasks = select_config(all_tasks, task_names, include_disabled=bool(task_names))
    if not tools:
        raise SystemExit("no enabled tools selected")
    if not tasks:
        raise SystemExit("no enabled tasks selected")

    # Named repositories that are disabled are still allowed; this lets a user
    # run a single disabled repo deliberately.
    if repo_names:
        repos = select_config(all_repos, repo_names, include_disabled=True)
    else:
        repos = select_config(all_repos)

    instances = _select_instances(manifest, args, tasks, repos)
    if not instances:
        raise SystemExit("no instances selected; run `bench.py mine` or widen filters")

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    args.run_id = run_id
    run_dir = home.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    if results_path.exists() and not (args.resume or args.fresh):
        raise SystemExit(
            f"{results_path} already exists; pass --resume to continue it or --fresh to replace it"
        )
    if args.fresh and results_path.exists():
        results_path.unlink()

    completed: set[tuple[str, str]] = set()
    if args.resume and results_path.exists():
        for row in load_rows(results_path):
            if not row.get("error"):
                completed.add((row.get("instance_id"), row.get("tool")))

    top_k = int(args.top_k or tool_defaults.get("top_k", 10))
    args.top_k = top_k

    # Materialise per-tool configs (defaults merged) and drop tools that cannot
    # answer any selected task.
    active: dict[str, dict] = {}
    unsupported: dict[str, list[str]] = {}
    for name, cfg in tools.items():
        merged = normalize_tool(cfg, tool_defaults)
        supported_tasks = [
            task for task, task_cfg in tasks.items()
            if query_for_task(merged, task, task_cfg) is not None
        ]
        if not supported_tasks:
            unsupported[name] = list(tasks)
            continue
        active[name] = merged
        missing = [task for task in tasks if task not in supported_tasks]
        if missing:
            unsupported[name] = missing
    if not active:
        raise SystemExit("none of the selected tools supports the selected tasks")

    meta = {
        "version": 1,
        "run_id": run_id,
        "started_at": now_iso(),
        "home": str(home.root),
        "manifest": str(home.manifest_path),
        "manifest_sha256": _sha256(home.manifest_path) if home.manifest_path.exists() else None,
        "instances": len(instances),
        "tasks": list(tasks),
        "tools": list(active),
        "repos": sorted({instance["repo"] for instance in instances}),
        "top_k": top_k,
        "query_mode": args.query_mode,
        "max_query_chars": args.max_query_chars,
        "jobs": args.jobs,
        "tool_config": {name: active[name] for name in active},
        "tool_versions": {},
        "repo_heads": {},
        "unsupported_tools": unsupported,
    }
    for name, cfg in active.items():
        meta["tool_versions"][name] = tool_version(cfg) or (
            which(_tool_binary(cfg)) or "unknown"
        )
    for repo_name in meta["repos"]:
        repo_cfg = repos.get(repo_name) or {}
        try:
            meta["repo_heads"][repo_name] = gitutil.head_commit(home.repo_path(repo_cfg))
        except Exception:
            meta["repo_heads"][repo_name] = "unknown"
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # Group by tree so one index build serves every task/instance on that commit.
    groups: dict[tuple[str, str], list[dict]] = {}
    for instance in instances:
        groups.setdefault((instance["repo"], instance["base_commit"]), []).append(instance)

    print(
        f"run {run_id}: {len(instances)} instances x {len(active)} tools "
        f"({len(groups)} base commits); tasks={','.join(tasks)}"
    )
    if unsupported:
        for name, missing in unsupported.items():
            print(f"  note: {name} not configured for {', '.join(missing)}")

    work_root = home.work_dir / run_id

    def run_group(group_key, group_instances):
        repo_name, base_commit = group_key
        repo_cfg = repos.get(repo_name) or {}
        repo_path = home.repo_path(repo_cfg)
        rows: list[dict] = []
        for tool_name, tool_cfg in active.items():
            todo = [
                instance for instance in group_instances
                if (instance["instance_id"], tool_name) not in completed
                and query_for_task(tool_cfg, instance["task"], tasks[instance["task"]]) is not None
            ]
            if not todo:
                continue
            worktree = (
                work_root
                / f"{repo_name}-{base_commit[:8]}"
                / tool_name.replace("/", "_")
            )
            prepare_s = None
            prepare_error = None
            try:
                gitutil.make_worktree(repo_path, base_commit, worktree)
                first_ctx = build_context(
                    todo[0], todo[0]["task"], tasks[todo[0]["task"]],
                    tool_cfg, args, worktree, repo_cfg,
                )
                try:
                    prepare_s = prepare_tool(tool_cfg, worktree, first_ctx)
                except Exception as exc:
                    prepare_error = f"{type(exc).__name__}: {exc}"[:1200]
                for instance in todo:
                    row = {
                        "instance_id": instance["instance_id"],
                        "task": instance["task"],
                        "repo": instance["repo"],
                        "tool": tool_name,
                        "base_commit": instance["base_commit"],
                        "fix_commit": instance.get("fix_commit"),
                        "error": None,
                        "prepare_s": prepare_s,
                        "query_s": None,
                        "metrics": None,
                        "top_paths": [],
                    }
                    if prepare_error:
                        row["error"] = prepare_error
                    else:
                        try:
                            record = execute_tool(
                                tool_name, tool_cfg, instance["task"],
                                tasks[instance["task"]], instance,
                                worktree, repo_cfg, args, prepare_s=prepare_s,
                            )
                            row["query_s"] = record["query_s"]
                            row["top_paths"] = unique_paths(record["results"], 10)
                            row["metrics"] = score_instance(
                                tasks[instance["task"]].get("scorer", "localization"),
                                record["results"], instance, top_k, worktree,
                            )
                        except Exception as exc:
                            row["error"] = f"{type(exc).__name__}: {exc}"[:1200]
                    rows.append(row)
                    status = "ok" if not row["error"] else "ERR"
                    detail = (
                        f"rank={row['metrics'].get('rank')}"
                        if row["metrics"] else row["error"][:80]
                    )
                    print(
                        f"    [{status}] {instance['instance_id']} "
                        f"<{tool_name}> {detail}"
                    )
            except Exception as exc:
                # Worktree creation or other harness failure: record it for
                # every pending instance so the run remains auditable.
                for instance in todo:
                    rows.append({
                        "instance_id": instance["instance_id"],
                        "task": instance["task"],
                        "repo": instance["repo"],
                        "tool": tool_name,
                        "base_commit": instance["base_commit"],
                        "fix_commit": instance.get("fix_commit"),
                        "error": f"{type(exc).__name__}: {exc}"[:1200],
                        "prepare_s": prepare_s,
                        "query_s": None,
                        "metrics": None,
                        "top_paths": [],
                    })
            finally:
                if not args.skip_cleanup and not args.keep_worktrees:
                    try:
                        ctx = build_context(
                            todo[0], todo[0]["task"], tasks[todo[0]["task"]],
                            tool_cfg, args, worktree, repo_cfg,
                        )
                        cleanup_tool(tool_cfg, worktree, ctx)
                    except Exception:
                        pass
                if not args.keep_worktrees:
                    gitutil.remove_worktree(repo_path, worktree)
        if rows:
            append_rows(results_path, rows)
        return rows

    all_rows: list[dict] = []
    if args.jobs <= 1:
        for group_key, group_instances in groups.items():
            all_rows.extend(run_group(group_key, group_instances))
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(run_group, key, value): key for key, value in groups.items()
            }
            for future in as_completed(futures):
                all_rows.extend(future.result())

    meta["finished_at"] = now_iso()
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"results: {results_path}")

    if not args.no_report:
        from .reporting import write_report

        write_report(home, run_dir, args.baseline, top_k)
    return run_dir
