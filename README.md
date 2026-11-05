# Code graph benchmark

A configurable, repeatable benchmark for code-graph and code-discovery tools.

The harness is **Python standard library only**, works on macOS/Linux/Windows,
and uses **no LLM judge and no AI agent** in the measurement loop. Ground truth
comes from local git history and a deterministic, regex-based source index.

```
benchmark-codegraph-tools/
├── bench.py                 # CLI: init | doctor | repos | tools | tasks | mine | run | report | runs
├── benchkit/                # harness implementation (stdlib only)
│   ├── cli.py               # argument parsing
│   ├── config.py            # home layout + JSON config I/O
│   ├── config_cmds.py       # repos/tools/tasks add/remove/enable/disable
│   ├── defaults.py          # shipped default configs
│   ├── doctor.py            # validation + tool x task matrix
│   ├── process.py           # cross-platform subprocess + timeout kill
│   ├── gitutil.py           # git log/diff/worktrees
│   ├── text.py              # issue cleaning, keywords, path normalisation
│   ├── languages.py         # symbol/call/import index (Java, Python, JS/TS, Go)
│   ├── mining.py            # mine instances from git history
│   ├── parsers.py           # tool output parsers (JSON, cbm, lines, ...)
│   ├── runners.py           # built-in Python runners (rg, cbm fusion)
│   ├── adapters.py          # render commands, run prepare/query
│   ├── scoring.py           # localization / site / set scoring
│   ├── run.py               # grouped execution + results.jsonl
│   ├── reporting.py         # summary.md/json/csv
│   └── plugins.py           # "module:function" plugin loader
├── config/
│   ├── repos.json           # repositories (local paths)
│   ├── tools.json           # tool adapters (enabled/disabled)
│   └── tasks.json           # task types + mining settings
├── manifest.json            # generated ground truth (the benchmark itself)
├── cache/gh/                # cached GitHub issue/PR payloads
├── results/<run-id>/        # results.jsonl, summary.md/json/csv, run.json
├── work/                    # throw-away git worktrees
└── tests/                   # unit + end-to-end tests
```

## Quick start

```bash
cd benchmark-codegraph-tools

# 1. Create config/repos.json, config/tools.json, config/tasks.json
python3 bench.py init

# 2. Validate binaries, repo paths, configs and the manifest
python3 bench.py doctor

# 3. Mine instances from local git history
#    (online issue text for localization; structural tasks also work offline)
python3 bench.py mine --repo spring-petclinic-microservices \
    --tasks localization,callers,symbol_definition --limit 4
python3 bench.py mine --repo BroadleafCommerce \
    --tasks localization,callers,symbol_definition --limit 4 --offline

# 4. Run every enabled tool that supports the selected tasks
python3 bench.py run --tasks localization,callers,symbol_definition \
    --run-id baseline

# 5. Rebuild a report from results (e.g. after changing a scoring rule)
python3 bench.py report --run baseline
```

A run writes:

- `results/<id>/results.jsonl` — one row per instance x tool
- `results/<id>/summary.md` — human-readable tables
- `results/<id>/summary.json` — machine-readable aggregates
- `results/<id>/summary.csv` and `results.csv` — spreadsheets
- `results/<id>/run.json` — tool versions, repo HEADs, query policy, config hash

A starter `manifest.json` (15 instances mined from the two default repositories)
is included so `bench.py doctor` and `bench.py run` work immediately. Delete it
or re-run `bench.py mine` to refresh the instance set.

## Configuration

Edit the JSON files directly, or use the CLI. All three configs have an
`enabled` flag; disabled entries stay in the file but are not used unless you
name them explicitly on the command line.

### Repositories — `config/repos.json`

Only **local paths** are used. The framework never clones anything.

```json
{
  "version": 1,
  "repos": {
    "my-java-app": {
      "enabled": true,
      "path": "../my-java-app",
      "language": "java",
      "include": ["**/*.java"],
      "exclude": ["**/src/test/**", "**/target/**"],
      "test_markers": ["**/src/test/**"],
      "max_file_bytes": 2000000
    }
  }
}
```

Relative paths are resolved from the benchmark home (the directory containing
`bench.py`). Add/remove repositories with:

```bash
python3 bench.py repos add --name my-app --path /src/my-app --language java
python3 bench.py repos add --name py-app --path ../py-app --language python \
    --include '**/*.py' --exclude '**/tests/**'
python3 bench.py repos disable --name my-app
python3 bench.py repos enable  --name my-app
python3 bench.py repos remove  --name my-app          # refuses if manifest instances exist
python3 bench.py repos remove  --name my-app --force
python3 bench.py repos list
```

### Tools — `config/tools.json`

Every tool is a declarative adapter. The shipped defaults keep
**`codegraph`** and **`codebase-memory-mcp`** enabled, plus `rg` as a keyword
baseline. `semble`, `semble-graph`, and `code-search` are included but disabled
as examples of optional adapters.

```json
{
  "version": 1,
  "defaults": {"top_k": 10, "timeout": 300, "prepare_timeout": 900},
  "plugins": ["./plugins"],
  "tools": {
    "my-tool": {
      "enabled": true,
      "kind": "symbol-graph",
      "binary": "my-tool",
      "cwd": "{repo}",
      "prepare": ["my-tool", "index", "{repo}"],
      "prepare_timeout": 900,
      "cleanup": ["my-tool", "drop-index", "{repo}"],
      "version": ["my-tool", "--version"],
      "has_lines": true,
      "parse": "recursive_json",
      "queries": {
        "localization": ["my-tool", "search", "{query}", "--limit", "{k}", "--json"],
        "symbol_definition": ["my-tool", "symbol", "{symbol}", "--json"],
        "callers": ["my-tool", "callers", "{symbol}", "--json"],
        "callees": ["my-tool", "callees", "{symbol}", "--json"],
        "impact": ["my-tool", "impact", "{symbol}", "--depth", "{depth}", "--json"],
        "dependents": ["my-tool", "importers", "{path}", "--json"]
      }
    }
  }
}
```

A query value can be a command list or an object with per-task overrides:

```json
"callers": {
  "argv": ["my-tool", "callers", "{symbol}", "--json"],
  "parse": "my_plugin:parse_my_tool",
  "runner": "my_plugin:run_my_tool",
  "timeout": 120,
  "cwd": "{repo}"
}
```

A tool participates in a task only if it has a `queries.<task>` entry (or a
top-level `query` and the task sets `allow_default_query`). Missing tasks are
reported as “not configured”, never as a fake zero.

Add/remove tools:

```bash
# File may contain {name: config}, {"tools": {...}}, or one config with "name"
python3 bench.py tools add --file my-tool.json
python3 bench.py tools add --file my-tool.json --force
python3 bench.py tools remove --name my-tool
python3 bench.py tools disable --name semble
python3 bench.py tools enable  --name semble
python3 bench.py tools list
python3 bench.py tools validate --name my-tool
```

#### Command placeholders

| Placeholder | Meaning |
|---|---|
| `{repo}` | absolute path to the per-instance worktree |
| `{repo_name}` | configured repository name |
| `{query}` | issue text or structural query, depending on task |
| `{symbol}` | symbol name for structural symbol tasks |
| `{qualified_symbol}` | `FileStem.symbol` hint for disambiguation |
| `{symbol_kind}` | `method`, `function`, `class`, ... |
| `{symbol_path}` / `{symbol_line}` | declaration file and line |
| `{path}` / `{file}` | repository-relative file path |
| `{path_basename}` | basename of `{path}` |
| `{path_stem}` | basename of `{path}` without its extension (e.g. `OrderDao`) |
| `{path_dir}` | basename of the directory containing `{path}` (e.g. `model`) |
| `{path_package}` | source-relative package path (e.g. `org/example/model`) |
| `{source}` / `{target}` | source and target symbols for path tasks |
| `{k}` / `{top_k}` | result limit |
| `{keywords}` | space-separated identifiers from the query |
| `{keywords_regex}` | `a|b|c` identifier regex |
| `{name_pattern}` | `.*(a|b|c).*` symbol-name regex |
| `{file_pattern}` | basename glob from the first repository `include` (e.g. `*.java`) |
| `{semantic_query}` | JSON-encoded query list for `search_graph --semantic-query` |
| `{depth}` | impact traversal depth |
| `{language}` | language of the target file |
| `{run_id}`, `{instance_id}` | run metadata |

#### Built-in parsers

- `recursive_json` (default) — walks arbitrary JSON and extracts any object with
  a path key (`filePath`, `file_path`, `path`, `file`, ...) and optional line
  keys (`startLine`, `line`, `lines`, `range`, ...). Handles `endLine`.
- `semble`, `code_search`, `codegraph` — named aliases/shapes.
- `cbm` — codebase-memory `cols`/`rows` tables and `groups` (search_graph).
- `cbm_query` — codebase-memory `query_graph` columns/rows.
- `codegraph_callers` / `codegraph_callees` — CodeGraph relation arrays,
  excluding the target definition (which generic JSON walking would wrongly
  count as a result).
- `codegraph_affected` — CodeGraph impact `affected` array, excluding roots.
- `codegraph_dependents` — CodeGraph file-mode "used by N files" header.
- `lines` / `text_paths` — plain-text output containing file paths.
- `jsonl` — one JSON value per line.

Any parser can be a plugin: `"parse": "my_module:my_parser"` with signature
`(stdout, repo, cfg) -> [item, ...]`.

#### Built-in runners

Runners are Python functions for tools that are not a single command:

- `rg_localization` — weighted distinct identifiers (the keyword baseline).
- `rg_symbol` — literal symbol occurrences.
- `rg_references` — literal identifier occurrences with lines.
- `rg_callers` — textual call-like occurrences.
- `cbm_fusion` — per-term `search_code` coverage fused with
  `search_graph --semantic-query` using weighted reciprocal-rank fusion for
  issue-text localization.
- `cbm_references` — merges graph `CALLS`/`USAGE` edges with `search_code`
  matches so declarations, imports, and annotations are not lost.

A runner receives `(query_cfg, repo, ctx, repo_cfg, timeout)` and returns
`(items, elapsed_seconds)`. Register custom runners as `"runner":
"my_module:my_runner"`.

### Tasks — `config/tasks.json`

Tasks are data too. The shipped set:

| Task | Query | Gold truth | Scorer |
|---|---|---|---|
| `localization` | issue/PR text | files + hunks changed by the fix | `localization` |
| `symbol_definition` | symbol name | declaration site at `base_commit` | `site` |
| `symbol_definitions` | ambiguous symbol name | **every** declaration of that name | `sites` |
| `callers` | symbol name | caller files, caller sites, call edges | `set` |
| `callees` | symbol name | callee files, callee sites, call edges | `set` |
| `impact` | symbol name | files/sites affected through transitive callers | `set` |
| `dependents` | file path | files importing it | `set` |
| `references` | symbol name | every non-declaration occurrence (calls, fields, types) | `set` |
| `implementations` | type name | all direct/transitive subtypes | `set` |
| `overrides` | method name | overriding methods in subtypes | `set` |
| `no_callers` | symbol name | empty set (negative control) | `absence` |

Example task definition:

```json
{
  "version": 1,
  "mining": {
    "since": "2022-01-01",
    "scan": 400,
    "max_files": 6,
    "max_lines": 400,
    "min_lines": 2,
    "min_problem_chars": 200,
    "exclude_subject": "(?i)\\b(upgrade|bump|refactor|cleanup|migrate)\\b",
    "symbols_per_commit": 1
  },
  "tasks": {
    "callers": {
      "enabled": true,
      "miner": "structural",
      "scorer": "set",
      "query_source": "structural",
      "query_template": "Find all callers of `{symbol}`.",
      "metrics": ["recall@5", "recall@10", "all@10", "hit@10", "precision@10", "mrr@10"],
      "symbols_per_commit": 1,
      "min_gold_files": 1,
      "max_gold_files": 30
    }
  }
}
```

Add/remove tasks:

```bash
python3 bench.py tasks add --file my-task.json
python3 bench.py tasks remove --name callers --force
python3 bench.py tasks disable --name impact
python3 bench.py tasks list
```

#### Custom / explicit instances

For a task whose ground truth you already have, use `--import` with a JSON file:

```json
{
  "instances": [{
    "instance_id": "my-task-001",
    "task": "my_task",
    "repo": "my-java-app",
    "base_commit": "<sha>",
    "query": {"text": "Where is Foo handled?"},
    "gold": {"files": ["src/Foo.java"], "sites": [{"path": "src/Foo.java", "line": 10}]},
    "meta": {}
  }]
}
```

```bash
python3 bench.py mine --import my-instances.json
```

Define the matching task in `tasks.json` with any built-in scorer (`site` or
`set`) and metrics, or register a custom scorer in `benchkit/scoring.py` /
a plugin.

## What an instance is

### Localization

For a fix commit `F` with parent `B`:

| Field | Meaning |
|---|---|
| `base_commit = B` | the tree a tool may see |
| `problem_statement` | issue/PR title + body (or commit message offline) |
| `gold.files` | production files changed by `F` |
| `gold.hunks` | changed line ranges |

Mining rules:

- non-merge commits whose subject references `#123` or `owner/repo#123`
- 1–`max_files` gold files, `min_lines`–`max_lines` changed lines
- gold files must already exist at `B`
- test files are excluded by repo `exclude` / `test_markers`
- subjects matching `exclude_subject` are skipped
- duplicate problem statements are dropped
- `flags.mentions_gold_path` records whether the issue names a gold path

### Structural

Structural instances are derived from the same candidate fix commits. For each
changed file, the harness picks a declaration that overlaps a changed hunk, then
uses the **base tree** to derive ground truth:

- `symbol_definition` — the declaration site itself.
- `symbol_definitions` — every declaration of an ambiguous name (overloads,
  same method in multiple classes); unlike the other tasks this deliberately
  keeps ambiguous targets.
- `callers` — caller symbols, caller sites, and `(file, caller, callee)` edges.
- `callees` — callee symbols, callee sites, and `(file, caller, callee)` edges.
- `impact` — transitive callers up to `impact_depth`, including edge lines.
- `dependents` — files whose import statements resolve to the target file.
- `references` — every non-declaration identifier occurrence, not just calls.
- `implementations` — direct/transitive `extends`/`implements` subtypes.
- `overrides` — methods in subtypes with the same name as the target method.
- `no_callers` — negative controls whose gold set is empty.

The source index masks comments/strings, matches declarations with regex,
finds brace/indent ranges, parses imports and type headers, and resolves Java
imports by fully qualified name. It supports Java, Python,
JavaScript/TypeScript, and basic Go. This is deterministic and cross-platform,
but it is **heuristic**, not a compiler or language server. Reports label these
tasks accordingly.

For tasks other than `symbol_definitions`, targets are excluded when their name
is ambiguous, when they are generic names (`main`, `get`, `toString`, ...), or
when the gold set is empty/too large. `symbol_definitions` exists specifically
to exercise that disambiguation path.

## What a run does

For each selected instance, the harness groups work by `(repo, base_commit)`.
For each tool in the group it:

1. creates a fresh detached git worktree under `work/<run-id>/`;
2. runs the tool's `prepare` command once (index build), timed separately;
3. runs each instance query in that same worktree, timed;
4. scores results against gold;
5. runs optional `cleanup` (e.g. delete the codebase-memory project);
6. removes the worktree.

A fresh worktree per tool matters: several tools write index directories
(`.codegraph`, `.context`, `.codebase-memory`) and must not see each other's
artifacts. Grouping by base commit means a 6-task, 6-tool, 5-instance run does
not rebuild an index for every task.

Query policy is explicit and recorded in `run.json`:

- `--query-mode issue` (default): `problem_statement` trimmed at PR-template
  headings, truncated to `--max-query-chars` (default 500).
- `--query-mode title`: title only.
- Structural tasks always use their `query_template`.
- `--top-k` is passed as each tool's limit (via `{k}`).
- `--repeat N` runs each query N times and reports median plus p95.
- `--split train|test` restricts the run to the deterministic held-out split
  stored in `instance.meta.split`.
- Repository `include`/`exclude` globs are enforced on returned paths by
  default. `--no-scope-filter` keeps out-of-scope results; the report shows how
  many were removed per tool (`OOS`).
- `--impact-depth` overrides the configured transitive depth.

## Metrics

| Metric | Meaning |
|---|---|
| `recall@k` (localization) | at least one gold file in the top-k unique files |
| `recall@k` (set tasks) | fraction of gold files found in the top-k unique files |
| `hit@k` | at least one gold file/site found in top-k |
| `all@k` | every gold file found in top-k |
| `precision@k` | found gold files / returned unique files |
| `mrr@10` | 1 / rank of the first gold match |
| `hunk@5` | a returned line range overlaps a gold hunk within top 5 (localization) |
| `site_recall@k` | fraction of gold declaration/reference sites returned |
| `site_precision@k` | fraction of returned items that match a gold site |
| `all_sites@k` | every gold site returned (`symbol_definitions`) |
| `edge_recall@k` / `edge_precision@k` / `edge_f1@k` | call/reference edges, keyed by `(file, caller, callee)` |
| `abstain@k` / `clean@k` | negative control returned no files |
| `false_positives@k` | files returned for a no-callers instance |
| `Files` / `Lines` | median unique files and returned line count (context cost) |
| `Prep s` / `Query s` / `Q p95` | median prepare/query seconds and query p95 |
| `Invalid` / `OOS` | paths absent from the worktree / removed by scope filtering |
| `Errors` | failed (instance, tool) pairs |

`summary.md` also includes per-repository tables, per-instance matrices,
paired comparisons against `--baseline` (default `rg`), tool errors, and
instances that every successful tool missed.

## Repeatability

- `manifest.json` is the benchmark; keep it with the run.
- Instances carry a deterministic `meta.split` (`train`/`test`, 20% held out by
  default). Use `--split test` for a held-out evaluation; `--holdout-fraction`
  and `--split-seed` control the assignment at mining time.
- `run.json` records tool versions, repository HEADs, query policy, repeat
  count, split, scope-filter state, manifest SHA-256, and the full tool config.
- Re-running into an existing `--run-id` is refused; use `--resume` to continue
  or `--fresh` to replace.
- Indices are rebuilt per instance/tool; nothing is reused between runs.
- GitHub metadata is cached in `cache/gh/`; use `--offline` for fully local
  mining (commit-message problem statements).
- `--jobs > 1` parallelizes base commits. Timing becomes noisy and indexers may
  contend; keep `--jobs 1` for numbers you intend to compare.
- Changing `--top-k`, `--query-mode`, or `--max-query-chars` changes the task:
  use a new run id.

## Cross-platform notes

- Commands are always argv lists; no shell quoting/expansion.
- UTF-8 decoding uses replacement so tool output cannot crash a run.
- Timeouts kill the whole process group (POSIX) or process tree (Windows
  `taskkill`).
- Paths are normalised to POSIX-style repository-relative paths.
- Relative repo paths resolve from the benchmark home.
- Use `--home` or `BENCH_HOME` to isolate a workspace (useful on CI and for
  experiments).
- Windows long-path and antivirus interference with `git worktree` are the most
  common issues; keep the benchmark directory short and exclude it from
  real-time scanning.

## Plugins

Reference a Python callable as `"module:function"` for a parser, runner, or
scorer. Add extra module directories in `config/tools.json`:

```json
"plugins": ["./plugins", "../shared-bench-plugins"]
```

A `<home>/plugins` directory is also on the path. No packaging or entry points
are required.

## Limitations

- Localization gold is whatever the fix commit changed in production files;
  fixes touching only tests/resources/build files are not instances.
- Structural gold is regex-derived, not compiler-grade. Generic method names,
  dynamic dispatch, reflection, and cross-language calls are invisible.
  `symbol_definitions`, `implementations`, and `overrides` mitigate this by
  testing ambiguous names and type hierarchies, but the oracle is still
  heuristic.
- `references` is an identifier-occurrence index, not a compiler-grade
  symbol/reference resolver; shadowed names and overloaded names can widen gold.
- `dependents` uses import statements only; same-package usage without imports
  is not counted.
- JavaScript/TypeScript resolves relative imports (including `.jsx`/`.tsx` and
  `index` files) and treats `<Component />` renders as call edges. tsconfig /
  webpack path aliases (`@/...`) are not resolved, `React.memo`/`forwardRef`
  wrappers are only seen when they contain a named function, and JSX text with
  an unescaped apostrophe can hide the rest of that line from the indexer.
- Negative controls (`no_callers`) make abstention and false positives visible;
  they do not replace a larger absence suite.
- The shipped manifest has only Java repositories. The index supports Python,
  JavaScript/TypeScript, and Go, but cross-language coverage requires adding
  local repositories and re-mining.
- Every run builds a fresh worktree and full index. Incremental indexing,
  post-edit `sync`, stale-cache behavior, and rename/delete handling are not yet
  measured.
- Codebase-memory's filtered index modes (`fast`, `moderate`) skip
  `**/samples/**`. On `spring-petclinic-microservices`, that is the entire Java
  package (`org.springframework.samples...`). The shipped adapter indexes with
  `--mode full` so samples and semantic data are available; if you switch back
  to a filtered mode, zero results on that repo are the tool's defaults, not a
  harness bug.
- Query formulation dominates localization results. Report the query policy
  alongside numbers and run both `issue` and `title` modes when comparing.
- Structural tasks measure graph traversal/localization, not patch resolution.
- Small N is directional. Add instances before treating differences as settled.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers masking, symbol/call/import/reference/hierarchy indexing,
JSON/cbm/CodeGraph parsers, file/site/edge/absence scoring, config add/remove,
and an end-to-end offline mine → run → report pipeline against a temporary git
repository.
