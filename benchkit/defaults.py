"""Shipped default configuration.

Run ``python3 bench.py init`` to materialise these files under ``config/``.
After that the JSON files are the source of truth; these dicts are only used to
create a fresh home (or to restore a missing file).
"""
from __future__ import annotations

from copy import deepcopy

DEFAULT_REPOS = {
    "version": 1,
    "repos": {
        "BroadleafCommerce": {
            "enabled": True,
            "path": "../BroadleafCommerce",
            "language": "java",
            "include": ["**/*.java"],
            "exclude": [
                "**/src/test/**",
                "**/src/it/**",
                "**/target/**",
                "**/build/**",
            ],
            "test_markers": ["**/src/test/**", "**/src/it/**"],
            "max_file_bytes": 2000000,
        },
        "spring-petclinic-microservices": {
            "enabled": True,
            "path": "../spring-petclinic-microservices",
            "language": "java",
            "include": ["**/*.java"],
            "exclude": [
                "**/src/test/**",
                "**/src/it/**",
                "**/target/**",
                "**/build/**",
            ],
            "test_markers": ["**/src/test/**", "**/src/it/**"],
            "max_file_bytes": 2000000,
        },
    },
}

DEFAULT_TASKS = {
    "version": 1,
    "mining": {
        "since": "2022-01-01",
        "scan": 400,
        "max_files": 6,
        "max_lines": 400,
        "min_lines": 2,
        "min_problem_chars": 200,
        "max_problem_chars": 8000,
        "exclude_subject": (
            "(?i)\\b(upgrade|bump|refactor|cleanup|clean up|migrate|"
            "reformat|typo|lint)\\b"
        ),
        "symbols_per_commit": 1,
        "holdout_fraction": 0.2,
        "split_seed": 0,
    },
    "tasks": {
        "localization": {
            "enabled": True,
            "description": (
                "Issue/PR text -> production files and line hunks changed by the "
                "fix commit. The original bench/ task, generalised to any "
                "configured repository and language."
            ),
            "miner": "git_issue",
            "scorer": "localization",
            "query_source": "issue",
            "allow_default_query": True,
            "metrics": [
                "recall@1", "recall@3", "recall@5", "recall@10",
                "mrr@10", "hunk@5",
            ],
        },
        "symbol_definition": {
            "enabled": True,
            "description": (
                "Given a symbol changed by a fix, return its definition file and "
                "line. Ground truth is the declaration site in the base tree."
            ),
            "miner": "structural",
            "scorer": "site",
            "query_source": "structural",
            "query_template": "Where is `{symbol}` defined?",
            "metrics": [
                "hit@1", "hit@3", "hit@5", "mrr@10",
                "site_recall@10", "site_precision@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 5,
        },
        "symbol_definitions": {
            "enabled": True,
            "description": (
                "Given a name with multiple declarations, list every definition. "
                "This exercises symbol identity and completeness instead of "
                "filtering ambiguous names out of the benchmark."
            ),
            "miner": "structural",
            "scorer": "sites",
            "query_source": "structural",
            "query_template": "List every definition of `{symbol}`.",
            "metrics": [
                "hit@1", "hit@3", "hit@5", "hit@10", "mrr@10",
                "site_recall@10", "site_precision@10", "all_sites@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 2,
            "max_gold_files": 20,
            "allow_ambiguous": True,
            "scan_all_symbols": True,
        },
        "callers": {
            "enabled": True,
            "description": (
                "Given a symbol changed by a fix, return the files that call it. "
                "Gold is derived from textual call sites at the base commit."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Find all callers of `{symbol}`.",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
                "site_recall@10", "site_precision@10",
                "edge_recall@10", "edge_precision@10", "edge_f1@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 30,
        },
        "callees": {
            "enabled": True,
            "description": (
                "Given a symbol changed by a fix, return the files that define "
                "the symbols it calls."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Find all functions called by `{symbol}`.",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
                "site_recall@10", "site_precision@10",
                "edge_recall@10", "edge_precision@10", "edge_f1@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 30,
        },
        "impact": {
            "enabled": True,
            "description": (
                "Given a symbol changed by a fix, return the files affected by "
                "changing it (transitive callers up to --impact-depth)."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "What code is affected if `{symbol}` changes?",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10", "mrr@10",
                "site_recall@10", "site_precision@10",
            ],
            "symbols_per_commit": 1,
            "impact_depth": 2,
            "min_gold_files": 1,
            "max_gold_files": 40,
        },
        "dependents": {
            "enabled": True,
            "description": (
                "Given a file changed by a fix, return the files that import it. "
                "Gold is derived from the base tree's import graph."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Which files import `{path}`?",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 40,
        },
        "references": {
            "enabled": True,
            "description": (
                "Given a symbol, return every non-declaration reference to it: "
                "calls, field/type uses, annotations, and other identifier "
                "occurrences. This is broader than callers."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Find every reference to `{symbol}`.",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
                "site_recall@10", "site_precision@10",
                "edge_recall@10", "edge_precision@10", "edge_f1@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 40,
        },
        "implementations": {
            "enabled": True,
            "description": (
                "Given a type, return every type that extends or implements it "
                "(direct and transitive), derived from class headers."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Find all types that extend or implement `{symbol}`.",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
                "site_recall@10", "site_precision@10",
                "edge_recall@10", "edge_precision@10", "edge_f1@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 40,
            "scan_all_symbols": True,
            "allow_ambiguous": True,
        },
        "overrides": {
            "enabled": True,
            "description": (
                "Given a method, return methods in subtypes that override it. "
                "This exercises type hierarchy plus method identity."
            ),
            "miner": "structural",
            "scorer": "set",
            "query_source": "structural",
            "query_template": "Find all methods that override `{symbol}`.",
            "metrics": [
                "recall@5", "recall@10", "all@10", "hit@10",
                "precision@10", "mrr@10",
                "site_recall@10", "site_precision@10",
                "edge_recall@10", "edge_precision@10", "edge_f1@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 1,
            "max_gold_files": 40,
            "scan_all_symbols": True,
            "allow_ambiguous": True,
        },
        "no_callers": {
            "enabled": True,
            "description": (
                "Negative control: the symbol has no callers. A successful tool "
                "must abstain instead of returning plausible files."
            ),
            "miner": "structural",
            "scorer": "absence",
            "query_source": "structural",
            "query_template": "Find all callers of `{symbol}`.",
            "metrics": [
                "abstain@10", "clean@10", "false_positives@10",
            ],
            "symbols_per_commit": 1,
            "min_gold_files": 0,
            "max_gold_files": 0,
            "allow_empty_gold": True,
            "scan_all_symbols": True,
        },
    },
}

DEFAULT_TOOLS = {
    "version": 1,
    "defaults": {
        "top_k": 10,
        "timeout": 300,
        "prepare_timeout": 900,
        "cleanup_timeout": 60,
        "scope_filter": True,
    },
    "plugins": [],
    "tools": {
        "codegraph": {
            "enabled": True,
            "kind": "symbol-graph",
            "description": "CodeGraph AST/graph index (query, callers, callees, impact)",
            "binary": "codegraph",
            "cwd": "{repo}",
            "prepare": ["codegraph", "init", "{repo}"],
            "prepare_timeout": 900,
            "timeout": 300,
            "version": ["codegraph", "-V"],
            "has_lines": True,
            "parse": "recursive_json",
            "queries": {
                "localization": [
                    "codegraph", "query", "{query}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "symbol_definition": [
                    "codegraph", "query", "{symbol}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "symbol_definitions": [
                    "codegraph", "query", "{symbol}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "references": [
                    "codegraph", "query", "{symbol}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "implementations": [
                    "codegraph", "query", "{symbol}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "overrides": [
                    "codegraph", "query", "{symbol}", "-p", "{repo}",
                    "-l", "{k}", "-j",
                ],
                "callers": {
                    "argv": [
                        "codegraph", "callers", "{symbol}", "-p", "{repo}",
                        "-l", "{k}", "-j",
                    ],
                    "parse": "codegraph_callers",
                },
                "no_callers": {
                    "argv": [
                        "codegraph", "callers", "{symbol}", "-p", "{repo}",
                        "-l", "{k}", "-j",
                    ],
                    "parse": "codegraph_callers",
                },
                "callees": {
                    "argv": [
                        "codegraph", "callees", "{symbol}", "-p", "{repo}",
                        "-l", "{k}", "-j",
                    ],
                    "parse": "codegraph_callees",
                },
                "impact": {
                    "argv": [
                        "codegraph", "impact", "{symbol}", "-p", "{repo}",
                        "-d", "{depth}", "-j",
                    ],
                    "parse": "codegraph_affected",
                },
                "dependents": {
                    "argv": [
                        "codegraph", "node", "-f", "{path}", "--symbols-only",
                        "-p", "{repo}",
                    ],
                    "parse": "codegraph_dependents",
                },
            },
        },
        "codebase-memory-mcp": {
            "enabled": True,
            "kind": "graph+keyword",
            "description": (
                "codebase-memory graph + literal/regex source search. "
                "Full indexes keep semantic data; localization fuses per-term "
                "search_code and semantic search_graph with weighted RRF."
            ),
            "binary": "codebase-memory-mcp",
            "cwd": "{repo}",
            "query_mode": "keywords",
            "prepare": [
                "codebase-memory-mcp", "cli", "index_repository",
                "--repo-path", "{repo}", "--mode", "full",
                "--persistence", "true",
            ],
            "prepare_timeout": 1800,
            "cleanup": [
                "codebase-memory-mcp", "cli", "delete_project",
                "--project", "{repo}",
            ],
            "timeout": 300,
            "version": ["codebase-memory-mcp", "--version"],
            "has_lines": True,
            "parse": "cbm",
            "queries": {
                "localization": {
                    "runner": "cbm_fusion",
                    "text_weight": 0.6,
                    "argv": [
                        "codebase-memory-mcp", "cli", "search_code",
                        "--project", "{repo}", "--pattern", "{keywords_regex}",
                        "--regex", "true", "--mode", "compact",
                        "--limit", "20", "--source-max-lines", "0",
                        "--file-pattern", "{file_pattern}",
                        "--format", "json",
                    ],
                    "semantic_argv": [
                        "codebase-memory-mcp", "cli", "search_graph",
                        "--project", "{repo}",
                        "--semantic-query", "{semantic_query}",
                        "--semantic-limit", "50", "--limit", "50",
                        "--format", "json",
                    ],
                    "parse": "cbm",
                },
                "symbol_definition": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "search_graph",
                        "--project", "{repo}", "--name-pattern", "^{symbol}$",
                        "--limit", "{k}", "--format", "json",
                    ],
                    "parse": "cbm",
                },
                "symbol_definitions": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "search_graph",
                        "--project", "{repo}", "--name-pattern", "^{symbol}$",
                        "--limit", "{k}", "--format", "json",
                    ],
                    "parse": "cbm",
                },
                "references": {
                    "runner": "cbm_references",
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:CALLS|USAGE|CALL_REFERENCE|MENTIONS]'
                        '->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name "
                        "LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                    "fallback_argv": [
                        "codebase-memory-mcp", "cli", "search_code",
                        "--project", "{repo}", "--pattern", "{symbol}",
                        "--regex", "true", "--mode", "compact",
                        "--limit", "20", "--source-max-lines", "0",
                        "--file-pattern", "{file_pattern}",
                        "--format", "json",
                    ],
                    "fallback_parse": "cbm",
                },
                "implementations": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:INHERITS|IMPLEMENTS]->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name "
                        "LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
                "overrides": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:OVERRIDE]->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name "
                        "LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
                "callers": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:CALLS]->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
                "no_callers": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:CALLS]->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
                "callees": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a {name:"{symbol}"})-[:CALLS]->(b) '
                        "RETURN b.name, b.file_path, b.start_line, a.name LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
                "impact": {
                    "argv": [
                        "codebase-memory-mcp", "cli", "query_graph",
                        "--project", "{repo}", "--query",
                        'MATCH (a)-[:CALLS*1..{depth}]->(b {name:"{symbol}"}) '
                        "RETURN a.name, a.file_path, a.start_line, b.name LIMIT {k}",
                        "--format", "json",
                    ],
                    "parse": "cbm_query",
                },
            },
        },
        "rg": {
            "enabled": True,
            "kind": "keyword",
            "description": (
                "ripgrep baseline: weighted identifiers for localization, "
                "literal symbol matches, and textual call-site search."
            ),
            "binary": "rg",
            "query_mode": "keywords",
            "timeout": 300,
            "version": ["rg", "--version"],
            "has_lines": False,
            "parse": "rg",
            "queries": {
                "localization": {"runner": "rg_localization"},
                "symbol_definition": {"runner": "rg_symbol"},
                "symbol_definitions": {"runner": "rg_symbol"},
                "references": {"runner": "rg_references"},
                "callers": {"runner": "rg_callers"},
                "no_callers": {"runner": "rg_callers"},
            },
        },
        "semble": {
            "enabled": False,
            "kind": "semantic",
            "description": "Semble chunk search (embeddings + BM25 hybrid)",
            "binary": "semble",
            "prepare": [
                "semble", "search", "warmup", "{repo}", "-k", "1",
                "--max-snippet-lines", "0",
            ],
            "prepare_timeout": 900,
            "timeout": 600,
            "version": ["semble", "-V"],
            "has_lines": True,
            "parse": "semble",
            "queries": {
                "localization": [
                    "semble", "search", "{query}", "{repo}", "-k", "{k}",
                    "--max-snippet-lines", "0", "--format", "json",
                    "--content", "code",
                ],
                "symbol_definition": [
                    "semble", "search", "{symbol}", "{repo}", "-k", "{k}",
                    "--max-snippet-lines", "0", "--format", "json",
                    "--content", "code",
                ],
            },
        },
        "semble-graph": {
            "enabled": False,
            "kind": "graph+semantic",
            "description": "Semble search expanded over the Semble graph sidecar",
            "binary": "semble-graph",
            "prepare": ["semble-graph", "build", "{repo}", "--format", "json"],
            "prepare_timeout": 900,
            "timeout": 600,
            "version": ["semble-graph", "-V"],
            "has_lines": True,
            "parse": "semble",
            "queries": {
                "localization": [
                    "semble-graph", "search", "{query}", "{repo}", "-k", "{k}",
                    "--max-snippet-lines", "0", "--format", "json",
                    "--content", "code",
                ],
            },
        },
        "code-search": {
            "enabled": False,
            "kind": "graph+semantic",
            "description": "code-search hybrid graph/vector/lexical engine",
            "binary": "code-search",
            "cwd": "{repo}",
            "prepare": ["code-search", "index", "--path", ".", "--no-daemon"],
            "prepare_timeout": 1800,
            "timeout": 600,
            "has_lines": True,
            "parse": "code_search",
            "queries": {
                "localization": [
                    "code-search", "search", "{query}", "--paths-only", "--json",
                    "--limit", "{k}", "--no-daemon", "--no-include-tests",
                    "--allow-weak", "--content", "code",
                ],
            },
        },
    },
}


def defaults_for(name: str) -> dict:
    return deepcopy({
        "repos": DEFAULT_REPOS,
        "tools": DEFAULT_TOOLS,
        "tasks": DEFAULT_TASKS,
    }[name])
