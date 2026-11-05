"""Validation command: configs, binaries, manifest and tool/task coverage."""
from __future__ import annotations

from . import gitutil
from .adapters import normalize_tool, query_for_task
from .config import BenchHome
from .parsers import PARSERS
from .process import binary_for, which
from .runners import RUNNERS
from .scoring import SCORERS


def validate(home: BenchHome) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    repos_payload = home.load_repos()
    tools_payload = home.load_tools()
    tasks_payload = home.load_tasks()
    repos = repos_payload.get("repos") or {}
    tools = tools_payload.get("tools") or {}
    tasks = tasks_payload.get("tasks") or {}

    for name, cfg in repos.items():
        path = home.repo_path(cfg)
        if not path.exists():
            errors.append(f"repo '{name}': path does not exist: {path}")
        elif not gitutil.is_git_repo(path):
            errors.append(f"repo '{name}': not a git repository: {path}")

    defaults = tools_payload.get("defaults") or {}
    for name, raw_cfg in tools.items():
        cfg = normalize_tool(raw_cfg, defaults)
        binary = binary_for(cfg, "prepare", "query")
        if binary and not which(binary):
            message = f"tool '{name}': executable not found: {binary}"
            if cfg.get("enabled", True):
                errors.append(message)
            else:
                warnings.append(message + " (tool is disabled)")
        parser = cfg.get("parse")
        if parser and parser not in PARSERS and ":" not in str(parser):
            errors.append(f"tool '{name}': unknown parser '{parser}'")
        for task, query in (cfg.get("queries") or {}).items():
            if isinstance(query, dict):
                if query.get("parse") and query["parse"] not in PARSERS and ":" not in str(query["parse"]):
                    errors.append(f"tool '{name}' / '{task}': unknown parser '{query['parse']}'")
                if query.get("runner") and query["runner"] not in RUNNERS and ":" not in str(query["runner"]):
                    errors.append(f"tool '{name}' / '{task}': unknown runner '{query['runner']}'")

    for name, cfg in tasks.items():
        scorer = cfg.get("scorer")
        if scorer not in SCORERS and ":" not in str(scorer):
            errors.append(f"task '{name}': unknown scorer '{scorer}'")
        if cfg.get("miner") not in {"git_issue", "localization", "structural", "import"}:
            errors.append(f"task '{name}': unknown miner '{cfg.get('miner')}'")
        if not cfg.get("metrics"):
            warnings.append(f"task '{name}': no metrics configured")

    instances = home.load_manifest().get("instances", [])
    for instance in instances:
        instance_id = instance.get("instance_id", "?")
        task = instance.get("task")
        repo = instance.get("repo")
        if task not in tasks:
            errors.append(f"{instance_id}: unknown task '{task}'")
        if repo not in repos:
            errors.append(f"{instance_id}: unknown repo '{repo}'")
            continue
        repo_path = home.repo_path(repos[repo])
        base = instance.get("base_commit")
        if not base or not gitutil.commit_exists(repo_path, base):
            errors.append(f"{instance_id}: base commit missing: {base}")
            continue
        gold = instance.get("gold") or {}
        files = gold.get("files") or []
        scorer = (tasks.get(task) or {}).get("scorer")
        if not files and scorer != "absence":
            errors.append(f"{instance_id}: no gold files")
        for path in files:
            if not gitutil.file_exists_at(repo_path, base, path):
                errors.append(f"{instance_id}: gold file not present at base: {path}")
        for site in gold.get("sites") or []:
            path = site.get("path")
            if path and not gitutil.file_exists_at(repo_path, base, path):
                errors.append(f"{instance_id}: gold site not present at base: {path}")
        for edge in gold.get("edges") or []:
            path = edge.get("path")
            if path and not gitutil.file_exists_at(repo_path, base, path):
                errors.append(
                    f"{instance_id}: gold edge path not present at base: {path}"
                )
        if not (instance.get("query") or {}).get("text") and not instance.get("problem_statement"):
            errors.append(f"{instance_id}: empty query")
        if task in tasks and tasks[task].get("scorer") in {"site", "sites"} and not (gold.get("sites") or []):
            errors.append(f"{instance_id}: site-scored task has no gold sites")
        if task in tasks and tasks[task].get("scorer") == "sites" and len(gold.get("sites") or []) < 2:
            errors.append(f"{instance_id}: 'sites' task needs at least two gold sites")
        if task in tasks and tasks[task].get("scorer") == "absence" and files:
            errors.append(f"{instance_id}: absence task must have an empty gold set")

    return errors, warnings


def cmd_doctor(home: BenchHome, args) -> None:
    repos_payload = home.load_repos()
    tools_payload = home.load_tools()
    tasks_payload = home.load_tasks()
    repos = repos_payload.get("repos") or {}
    tools = tools_payload.get("tools") or {}
    tasks = tasks_payload.get("tasks") or {}
    defaults = tools_payload.get("defaults") or {}

    print("Repositories")
    for name, cfg in sorted(repos.items()):
        path = home.repo_path(cfg)
        status = "ok" if path.exists() and gitutil.is_git_repo(path) else "BAD"
        print(f"  {status:4s} {name:34s} {path}")

    print("\nTools")
    for name, raw_cfg in sorted(tools.items()):
        cfg = normalize_tool(raw_cfg, defaults)
        binary = binary_for(cfg, "prepare", "query")
        status = "ok" if binary and which(binary) else "missing"
        print(
            f"  {status:8s} {name:28s} {str(bool(cfg.get('enabled', True))):5s} "
            f"{which(binary) or binary or '?'}"
        )

    print("\nTasks")
    for name, cfg in sorted(tasks.items()):
        print(
            f"  {str(bool(cfg.get('enabled', True))):5s} {name:22s} "
            f"{cfg.get('miner', '?'):12s} {cfg.get('scorer', '?'):12s} "
            f"{','.join(cfg.get('metrics') or [])}"
        )

    print("\nTool x task coverage")
    task_names = [name for name, cfg in tasks.items() if cfg.get("enabled", True)]
    tool_names = [name for name, cfg in tools.items() if cfg.get("enabled", True)]
    columns = task_names
    print(f"  {'tool':28s} " + " ".join(f"{name[:14]:14s}" for name in columns))
    for name in tool_names:
        cfg = normalize_tool(tools[name], defaults)
        cells = []
        for task in columns:
            cells.append("yes" if query_for_task(cfg, task, tasks[task]) else "-")
        print(f"  {name:28s} " + " ".join(f"{cell:14s}" for cell in cells))

    print("\nManifest")
    instances = home.load_manifest().get("instances", [])
    if not instances:
        print(f"  no instances at {home.manifest_path}; run `bench.py mine`")
    else:
        by_task: dict[str, int] = {}
        by_repo: dict[str, int] = {}
        for instance in instances:
            by_task[instance.get("task", "?")] = by_task.get(instance.get("task", "?"), 0) + 1
            by_repo[instance.get("repo", "?")] = by_repo.get(instance.get("repo", "?"), 0) + 1
        print(f"  {len(instances)} instances")
        print("  tasks: " + ", ".join(f"{key}={value}" for key, value in sorted(by_task.items())))
        print("  repos: " + ", ".join(f"{key}={value}" for key, value in sorted(by_repo.items())))

    errors, warnings = validate(home)
    if warnings:
        print("\nWarnings")
        for warning in warnings:
            print(f"  ! {warning}")
    if errors:
        print("\nErrors")
        for error in errors:
            print(f"  x {error}")
        print(f"\n{len(errors)} error(s)")
        if getattr(args, "strict", False):
            raise SystemExit(1)
    else:
        print("\nNo validation errors.")
