"""CLI implementations for init / repos / tools / tasks."""
from __future__ import annotations

from pathlib import Path

from .config import BenchHome, load_json, save_json
from .defaults import defaults_for
from .parsers import PARSERS
from .process import which
from .runners import RUNNERS
from .scoring import SCORERS


LANGUAGE_GLOBS = {
    "java": ["**/*.java"],
    "python": ["**/*.py"],
    "javascript": ["**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx"],
    "typescript": ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
    "go": ["**/*.go"],
    "mixed": ["**/*.java", "**/*.py", "**/*.js", "**/*.ts", "**/*.go"],
}


def cmd_init(home: BenchHome, args) -> None:
    home.ensure_dirs()
    targets = {
        home.repos_path(): defaults_for("repos"),
        home.tools_path(): defaults_for("tools"),
        home.tasks_path(): defaults_for("tasks"),
    }
    for path, payload in targets.items():
        if path.exists() and not args.force:
            print(f"exists  {path} (use --force to replace)")
            continue
        save_json(path, payload)
        print(f"wrote   {path}")
    print(f"home    {home.root}")


def _manifest_repos(home: BenchHome) -> set[str]:
    return {
        instance.get("repo")
        for instance in home.load_manifest().get("instances", [])
    }


def _manifest_tasks(home: BenchHome) -> set[str]:
    return {
        instance.get("task")
        for instance in home.load_manifest().get("instances", [])
    }


def cmd_repos(home: BenchHome, args) -> None:
    payload = home.load_repos()
    repos = payload.setdefault("repos", {})
    action = args.action

    if action == "list":
        if not repos:
            print("no repositories configured")
            return
        print(f"{'name':34s} {'enabled':8s} {'exists':8s} path")
        for name, cfg in sorted(repos.items()):
            path = home.repo_path(cfg)
            print(
                f"{name:34s} {str(bool(cfg.get('enabled', True))):8s} "
                f"{str(path.exists()):8s} {path}"
            )
        return

    if action == "add":
        if not args.name or not args.path:
            raise SystemExit("repos add requires --name and --path")
        if args.name in repos and not args.force:
            raise SystemExit(
                f"repository '{args.name}' already exists; pass --force to replace"
            )
        raw_path = Path(args.path).expanduser()
        if not raw_path.exists() and not args.allow_missing:
            raise SystemExit(f"path does not exist: {raw_path}")
        language = (args.language or "java").lower()
        includes = args.include or LANGUAGE_GLOBS.get(language, ["**/*"])
        excludes = args.exclude or [
            "**/src/test/**", "**/src/it/**", "**/target/**", "**/build/**",
        ]
        repos[args.name] = {
            "enabled": not args.disabled,
            "path": str(raw_path),
            "language": language,
            "include": includes,
            "exclude": excludes,
            "test_markers": ["**/src/test/**", "**/src/it/**"],
            **({"slug": args.slug} if args.slug else {}),
        }
        home.save_repos(payload)
        print(f"added {args.name} -> {raw_path}")
        return

    if not args.name:
        raise SystemExit(f"repos {action} requires --name")
    if args.name not in repos:
        raise SystemExit(f"unknown repository: {args.name}")

    if action == "remove":
        if args.name in _manifest_repos(home) and not args.force:
            raise SystemExit(
                f"'{args.name}' has manifest instances; pass --force to remove anyway"
            )
        del repos[args.name]
        home.save_repos(payload)
        print(f"removed {args.name}")
    elif action in {"enable", "disable"}:
        repos[args.name]["enabled"] = action == "enable"
        home.save_repos(payload)
        print(f"{action}d {args.name}")
    elif action == "set":
        cfg = repos[args.name]
        if args.path:
            cfg["path"] = args.path
        if args.language:
            cfg["language"] = args.language
        if args.include:
            cfg["include"] = args.include
        if args.exclude:
            cfg["exclude"] = args.exclude
        if args.slug:
            cfg["slug"] = args.slug
        if args.disabled:
            cfg["enabled"] = False
        home.save_repos(payload)
        print(f"updated {args.name}")
    else:
        raise SystemExit(f"unknown repos action: {action}")


def _tool_entries(payload) -> dict:
    if not isinstance(payload, dict):
        raise SystemExit("tool file must contain a JSON object")
    if "tools" in payload and isinstance(payload["tools"], dict):
        return payload["tools"]
    if "name" in payload and isinstance(payload.get("config"), dict):
        return {payload["name"]: payload["config"]}
    if any(key in payload for key in ("kind", "queries", "query", "prepare", "runner")):
        cfg = dict(payload)
        name = cfg.pop("name", None)
        if not name:
            raise SystemExit("single-tool file needs a 'name' field")
        return {name: cfg}
    return payload


def _validate_tool(name: str, cfg: dict) -> list[str]:
    problems = []
    if not isinstance(cfg, dict):
        return [f"{name}: config is not an object"]
    parser = cfg.get("parse")
    if parser and parser not in PARSERS and ":" not in str(parser):
        problems.append(f"{name}: unknown parser '{parser}'")
    queries = cfg.get("queries") or {}
    if not isinstance(queries, dict):
        problems.append(f"{name}: 'queries' must be an object")
    for task, query in queries.items():
        if isinstance(query, dict):
            query_parser = query.get("parse")
            if query_parser and query_parser not in PARSERS and ":" not in str(query_parser):
                problems.append(f"{name}/{task}: unknown parser '{query_parser}'")
            runner = query.get("runner")
            if runner and runner not in RUNNERS and ":" not in str(runner):
                problems.append(f"{name}/{task}: unknown runner '{runner}'")
    return problems


def cmd_tools(home: BenchHome, args) -> None:
    payload = home.load_tools()
    tools = payload.setdefault("tools", {})
    action = args.action

    if action == "list":
        tasks_payload = home.load_tasks().get("tasks") or {}
        task_names = sorted(tasks_payload)
        print(f"{'name':28s} {'on':3s} {'binary':38s} tasks")
        for name, cfg in sorted(tools.items()):
            binary = cfg.get("binary") or ""
            found = which(binary) or "MISSING"
            supported = [
                task for task in task_names
                if task in (cfg.get("queries") or {})
                or ("query" in cfg and (tasks_payload.get(task) or {}).get("allow_default_query"))
            ]
            print(
                f"{name:28s} {str(bool(cfg.get('enabled', True))):3s} "
                f"{found:38s} {','.join(supported) or '-'}"
            )
        return

    if action == "add":
        if not args.file:
            raise SystemExit("tools add requires --file")
        new_entries = _tool_entries(load_json(args.file, None))
        if args.name and len(new_entries) == 1:
            new_entries = {args.name: next(iter(new_entries.values()))}
        problems = []
        for name, cfg in new_entries.items():
            if name in tools and not args.force:
                raise SystemExit(
                    f"tool '{name}' already exists; pass --force to replace"
                )
            problems.extend(_validate_tool(name, cfg))
        if problems and not args.force:
            raise SystemExit("tool validation failed:\n  " + "\n  ".join(problems))
        tools.update(new_entries)
        home.save_tools(payload)
        print(f"added/replaced: {', '.join(new_entries)}")
        return

    if not args.name:
        raise SystemExit(f"tools {action} requires --name")
    if args.name not in tools:
        raise SystemExit(f"unknown tool: {args.name}")

    if action == "remove":
        del tools[args.name]
        home.save_tools(payload)
        print(f"removed {args.name}")
    elif action in {"enable", "disable"}:
        tools[args.name]["enabled"] = action == "enable"
        home.save_tools(payload)
        print(f"{action}d {args.name}")
    elif action == "validate":
        problems = _validate_tool(args.name, tools[args.name])
        if problems:
            raise SystemExit("\n".join(problems))
        print(f"{args.name}: ok")
    else:
        raise SystemExit(f"unknown tools action: {action}")


def cmd_tasks(home: BenchHome, args) -> None:
    payload = home.load_tasks()
    tasks = payload.setdefault("tasks", {})
    action = args.action

    if action == "list":
        print(f"{'name':22s} {'on':3s} {'miner':12s} {'scorer':14s} metrics")
        for name, cfg in sorted(tasks.items()):
            print(
                f"{name:22s} {str(bool(cfg.get('enabled', True))):3s} "
                f"{cfg.get('miner', '?'):12s} {cfg.get('scorer', '?'):14s} "
                f"{','.join(cfg.get('metrics') or [])}"
            )
        return

    if action == "add":
        if not args.file:
            raise SystemExit("tasks add requires --file")
        new_entries = load_json(args.file, None)
        if isinstance(new_entries, dict) and "tasks" in new_entries:
            new_entries = new_entries["tasks"]
        if not isinstance(new_entries, dict):
            raise SystemExit("task file must contain a JSON object of task -> config")
        if args.name and len(new_entries) == 1:
            new_entries = {args.name: next(iter(new_entries.values()))}
        problems = []
        for name, cfg in new_entries.items():
            if name in tasks and not args.force:
                raise SystemExit(
                    f"task '{name}' already exists; pass --force to replace"
                )
            if not isinstance(cfg, dict):
                problems.append(f"{name}: config is not an object")
                continue
            if cfg.get("scorer") not in SCORERS and ":" not in str(cfg.get("scorer")):
                problems.append(f"{name}: unknown scorer '{cfg.get('scorer')}'")
            if cfg.get("miner") not in {"git_issue", "localization", "structural", "import"}:
                problems.append(f"{name}: unknown miner '{cfg.get('miner')}'")
            if not cfg.get("metrics"):
                problems.append(f"{name}: no metrics configured")
        if problems and not args.force:
            raise SystemExit("task validation failed:\n  " + "\n  ".join(problems))
        tasks.update(new_entries)
        home.save_tasks(payload)
        print(f"added/replaced: {', '.join(new_entries)}")
        return

    if not args.name:
        raise SystemExit(f"tasks {action} requires --name")
    if args.name not in tasks:
        raise SystemExit(f"unknown task: {args.name}")

    if action == "remove":
        if args.name in _manifest_tasks(home) and not args.force:
            raise SystemExit(
                f"'{args.name}' has manifest instances; pass --force to remove anyway"
            )
        del tasks[args.name]
        home.save_tasks(payload)
        print(f"removed {args.name}")
    elif action in {"enable", "disable"}:
        tasks[args.name]["enabled"] = action == "enable"
        home.save_tasks(payload)
        print(f"{action}d {args.name}")
    else:
        raise SystemExit(f"unknown tasks action: {action}")
