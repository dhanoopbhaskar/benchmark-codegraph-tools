"""Command-line entry point."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import BenchHome, select_config
from .config_cmds import cmd_init, cmd_repos, cmd_tasks, cmd_tools
from .doctor import cmd_doctor
from .mining import load_imported_instances, merge_manifest, mine_repo
from .reporting import write_report
from .run import cmd_run


def _add_home(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home", default=None,
        help="benchmark home directory (default: $BENCH_HOME or the benchmark/ dir)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Configurable benchmark for code-graph and code-discovery tools. "
            "Stdlib only; no AI judge."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_home(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="write default configs")
    _add_home(p_init)
    p_init.add_argument("--force", action="store_true", help="replace existing config files")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="validate configs, binaries and manifest")
    _add_home(p_doctor)
    p_doctor.add_argument("--strict", action="store_true", help="exit 1 on validation errors")
    p_doctor.set_defaults(func=cmd_doctor)

    p_repos = sub.add_parser("repos", help="list/add/remove/enable/disable repositories")
    _add_home(p_repos)
    p_repos.add_argument("action", choices=["list", "add", "remove", "enable", "disable", "set"])
    p_repos.add_argument("--name")
    p_repos.add_argument("--path")
    p_repos.add_argument("--language", default=None)
    p_repos.add_argument("--include", action="append", help="include glob (repeatable)")
    p_repos.add_argument("--exclude", action="append", help="exclude glob (repeatable)")
    p_repos.add_argument("--slug", help="GitHub owner/name for issue lookup")
    p_repos.add_argument("--disabled", action="store_true", help="add/update as disabled")
    p_repos.add_argument("--force", action="store_true")
    p_repos.add_argument("--allow-missing", action="store_true")
    p_repos.set_defaults(func=cmd_repos)

    p_tools = sub.add_parser("tools", help="list/add/remove/enable/disable tool adapters")
    _add_home(p_tools)
    p_tools.add_argument("action", choices=["list", "add", "remove", "enable", "disable", "validate"])
    p_tools.add_argument("--name")
    p_tools.add_argument("--file", help="JSON file with one or more tool configs")
    p_tools.add_argument("--force", action="store_true")
    p_tools.set_defaults(func=cmd_tools)

    p_tasks = sub.add_parser("tasks", help="list/add/remove/enable/disable task types")
    _add_home(p_tasks)
    p_tasks.add_argument("action", choices=["list", "add", "remove", "enable", "disable"])
    p_tasks.add_argument("--name")
    p_tasks.add_argument("--file", help="JSON file with one or more task configs")
    p_tasks.add_argument("--force", action="store_true")
    p_tasks.set_defaults(func=cmd_tasks)

    p_mine = sub.add_parser("mine", help="mine instances from local git history")
    _add_home(p_mine)
    p_mine.add_argument("--repo", action="append", help="repository name (repeatable)")
    p_mine.add_argument("--tasks", "--task", dest="tasks", help="comma-separated task names")
    p_mine.add_argument("--limit", type=int, default=6, help="instances per task per repo")
    p_mine.add_argument("--since", default=None)
    p_mine.add_argument("--scan", type=int, default=None, help="max commits to inspect")
    p_mine.add_argument("--max-files", type=int, default=None)
    p_mine.add_argument("--max-lines", type=int, default=None)
    p_mine.add_argument("--min-lines", type=int, default=None)
    p_mine.add_argument("--min-problem-chars", type=int, default=None)
    p_mine.add_argument("--max-problem-chars", type=int, default=None)
    p_mine.add_argument("--exclude-subject", default=None)
    p_mine.add_argument("--symbols-per-commit", type=int, default=None)
    p_mine.add_argument("--offline", action="store_true", help="use commit messages, not GitHub")
    p_mine.add_argument("--import", dest="import_file", help="merge explicit instances from JSON")
    p_mine.add_argument("--dry-run", action="store_true")
    p_mine.add_argument("--manifest", help="override manifest path")
    p_mine.set_defaults(func=cmd_mine)

    p_run = sub.add_parser("run", help="run tools over instances")
    _add_home(p_run)
    p_run.add_argument("--tools", help="comma-separated tool names (default: all enabled)")
    p_run.add_argument("--tasks", "--task", dest="tasks", help="comma-separated task names")
    p_run.add_argument("--repo", action="append", help="repository name (repeatable)")
    p_run.add_argument("--instance", action="append", help="instance id (repeatable)")
    p_run.add_argument("--limit", type=int, default=0, help="max instances")
    p_run.add_argument("--top-k", type=int, default=None)
    p_run.add_argument("--query-mode", choices=["issue", "title"], default="issue")
    p_run.add_argument("--max-query-chars", type=int, default=500)
    p_run.add_argument("--jobs", type=int, default=1)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--resume", action="store_true")
    p_run.add_argument("--fresh", action="store_true")
    p_run.add_argument("--keep-worktrees", action="store_true")
    p_run.add_argument("--skip-cleanup", action="store_true")
    p_run.add_argument("--baseline", default="rg")
    p_run.add_argument("--no-report", action="store_true")
    p_run.add_argument("--manifest", help="override manifest path")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="rebuild a report from results")
    _add_home(p_report)
    p_report.add_argument("--run", required=True, help="run id or path")
    p_report.add_argument("--baseline", default="rg")
    p_report.add_argument("--top-k", type=int, default=None)
    p_report.add_argument("--manifest", help="override manifest path")
    p_report.set_defaults(func=cmd_report)

    p_runs = sub.add_parser("runs", help="list recorded runs")
    _add_home(p_runs)
    p_runs.set_defaults(func=cmd_runs)

    return parser


def _apply_manifest_override(home: BenchHome, args) -> None:
    manifest = getattr(args, "manifest", None)
    if manifest:
        home.manifest_path = Path(manifest).expanduser().resolve()


def cmd_mine(home: BenchHome, args) -> None:
    _apply_manifest_override(home, args)
    repos_payload = home.load_repos()
    tasks_payload = home.load_tasks()
    all_repos = repos_payload.get("repos") or {}
    all_tasks = tasks_payload.get("tasks") or {}
    mining_cfg = tasks_payload.get("mining") or {}

    repo_names = args.repo
    repos = select_config(all_repos, repo_names, include_disabled=bool(repo_names))
    if not repos:
        raise SystemExit("no enabled repositories; use `bench.py repos list`")

    task_names = [name.strip() for name in args.tasks.split(",") if name.strip()] if args.tasks else None
    tasks = select_config(all_tasks, task_names, include_disabled=bool(task_names))
    if not tasks:
        raise SystemExit("no enabled tasks")

    existing = home.load_manifest()
    all_new: list[dict] = []

    if args.import_file:
        imported = load_imported_instances(args.import_file)
        all_new.extend(imported)
        print(f"imported {len(imported)} instance(s) from {args.import_file}")

    mineable = {
        name: cfg for name, cfg in tasks.items()
        if cfg.get("miner") in {"localization", "git_issue", "structural"}
    }
    if mineable and not args.import_file:
        for repo_name, repo_cfg in repos.items():
            mined = mine_repo(home, repo_name, repo_cfg, mineable, args, mining_cfg)
            all_new.extend(mined)
            print(f"{repo_name}: mined {len(mined)} instance(s)")
    elif not mineable and not args.import_file:
        raise SystemExit("selected tasks have no built-in miner; use --import")

    if args.dry_run:
        print(f"\n{len(all_new)} instance(s); manifest not written")
        return

    manifest = existing
    by_repo: dict[str, list[dict]] = {}
    for instance in all_new:
        by_repo.setdefault(instance.get("repo", ""), []).append(instance)
    for repo_name, instances in by_repo.items():
        task_names_for_repo = sorted({instance.get("task") for instance in instances})
        manifest = merge_manifest(
            manifest, instances, repo_name, task_names_for_repo, str(home.root)
        )
    # Imported instances are merged by id, even when their repo is not local.
    imported_ids = {instance["instance_id"] for instance in all_new}
    if args.import_file:
        existing_instances = manifest.get("instances", [])
        merged = [
            instance for instance in existing_instances
            if instance.get("instance_id") not in imported_ids
        ] + all_new
        manifest["instances"] = merged

    manifest["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    home.save_manifest(manifest)
    print(f"\nwrote {len(manifest.get('instances', []))} instances to {home.manifest_path}")


def cmd_report(home: BenchHome, args) -> None:
    _apply_manifest_override(home, args)
    run_dir = Path(args.run)
    if not run_dir.is_absolute() and not run_dir.exists():
        run_dir = home.results_dir / args.run
    if not run_dir.exists():
        raise SystemExit(f"run not found: {run_dir}")
    write_report(home, run_dir, args.baseline, args.top_k)


def cmd_runs(home: BenchHome, args) -> None:
    if not home.results_dir.exists():
        print("no runs")
        return
    for run_dir in sorted(home.results_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        meta = {}
        meta_path = run_dir / "run.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        print(
            f"{run_dir.name:24s} instances={meta.get('instances', '?'):4} "
            f"tools={len(meta.get('tools') or []):2} "
            f"top_k={meta.get('top_k', '?'):3} {meta.get('query_mode', '')}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    home = BenchHome.resolve(getattr(args, "home", None))
    if args.cmd != "init":
        home.ensure_dirs()
    args.func(home, args)


if __name__ == "__main__":
    main()
