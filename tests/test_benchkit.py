"""Unit and end-to-end tests. Run with: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_ROOT))

from benchkit.adapters import query_for_task  # noqa: E402
from benchkit.config import BenchHome, load_json, save_json  # noqa: E402
from benchkit.config_cmds import cmd_repos, cmd_tools  # noqa: E402
from benchkit.languages import RepoIndex, mask_code  # noqa: E402
from benchkit.mining import assign_splits  # noqa: E402
from benchkit.text import path_in_scope  # noqa: E402
from benchkit.parsers import (  # noqa: E402
    parse_cbm,
    parse_cbm_query,
    parse_cbm_semantic,
    parse_codegraph_affected,
    parse_codegraph_callees,
    parse_codegraph_callers,
    parse_codegraph_dependents,
    parse_recursive_json,
)
from benchkit.scoring import (  # noqa: E402
    aggregate_rows,
    score_absence,
    score_localization,
    score_set,
    score_site,
    score_sites,
)


class MaskingTests(unittest.TestCase):
    def test_route_string_does_not_swallow_code(self):
        source = (
            'class A {\n'
            '    @PutMapping("/owners/*/pets/{petId}")\n'
            '    public void processUpdateForm(int id) {\n'
            '        findPetById(id);\n'
            '    }\n'
            '}\n'
        )
        masked = mask_code(source, "java")
        self.assertIn("processUpdateForm", masked)
        self.assertIn("findPetById", masked)
        self.assertNotIn("/owners", masked)

    def test_line_comments_are_blanked(self):
        masked = mask_code("class A {\n// hidden\nint x;\n}\n", "java")
        self.assertNotIn("hidden", masked)
        self.assertIn("int x;", masked)

    def test_strings_can_be_preserved_for_import_scans(self):
        source = (
            "import x from './x';\n"
            "const url = 'http://example.test';\n"
            "// hidden import { Nope } from './nope';\n"
        )
        masked = mask_code(source, "javascript", mask_strings=False)
        self.assertIn("'./x'", masked)
        self.assertIn("http://example.test", masked)
        self.assertNotIn("hidden", masked)
        self.assertNotIn("./nope", masked)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src/main/java/com/example").mkdir(parents=True)
        (self.root / "src/main/java/com/example/Foo.java").write_text(
            "package com.example;\n"
            "public class Foo {\n"
            "    public void target(int x) {\n"
            "        helper(x);\n"
            "    }\n"
            "    public void caller() {\n"
            "        target(1);\n"
            "    }\n"
            "    public void helper(int x) {\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "src/main/java/com/example/Bar.java").write_text(
            "package com.example;\n"
            "public class Bar {\n"
            "    public void other() {\n"
            "        new Foo().target(2);\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "src/main/java/com/example/dto").mkdir(parents=True)
        (self.root / "src/main/java/com/example/two/dto").mkdir(parents=True)
        (self.root / "src/main/java/com/example/dto/PetRequest.java").write_text(
            "package com.example.dto;\npublic class PetRequest {}\n", encoding="utf-8"
        )
        (self.root / "src/main/java/com/example/two/dto/PetRequest.java").write_text(
            "package com.example.two.dto;\npublic class PetRequest {}\n", encoding="utf-8"
        )
        (self.root / "src/main/java/com/example/User.java").write_text(
            "package com.example;\n"
            "import com.example.dto.PetRequest;\n"
            "public class User { PetRequest request; }\n",
            encoding="utf-8",
        )
        (self.root / "src/main/java/com/example/Base.java").write_text(
            "package com.example;\n"
            "public interface Base {\n"
            "    void handle();\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "src/main/java/com/example/Impl.java").write_text(
            "package com.example;\n"
            "public class Impl implements Base {\n"
            "    public void handle() { implHelper(1); }\n"
            "    public void implHelper(int x) {}\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "src/main/java/com/example/UseImpl.java").write_text(
            "package com.example;\n"
            "public class UseImpl {\n"
            "    public void go() { new Impl().handle(); }\n"
            "}\n",
            encoding="utf-8",
        )
        self.cfg = {
            "include": ["**/*.java"],
            "exclude": ["**/src/test/**"],
        }
        self.index = RepoIndex(self.root, self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_callers_and_callees(self):
        target = self.index.unique_symbol("target")
        self.assertIsNotNone(target)
        self.assertEqual(
            sorted(caller.name for caller in self.index.callers(target)),
            ["caller", "other"],
        )
        callees = [callee.name for callee in self.index.callees(target)]
        self.assertIn("helper", callees)

    def test_impact_is_transitive(self):
        target = self.index.unique_symbol("target")
        affected = {symbol.name for symbol in self.index.impact(target, depth=2)}
        self.assertTrue({"caller", "other"}.issubset(affected))

    def test_java_import_resolves_by_fqn(self):
        path = "src/main/java/com/example/dto/PetRequest.java"
        self.assertEqual(
            self.index.importers_of(path),
            ["src/main/java/com/example/User.java"],
        )

    def test_references_include_non_call_sites(self):
        target = self.index.unique_symbol("target")
        references = self.index.references(target)
        self.assertTrue(any(path.endswith("Bar.java") for _o, _l, path in references))

    def test_type_hierarchy_and_overrides(self):
        base = next(
            symbol for symbol in self.index.symbols_named("handle")
            if symbol.path.endswith("Base.java")
        )
        self.assertEqual(
            [item.name for item in self.index.implementations("Base")],
            ["Impl"],
        )
        overrides = self.index.overrides(base)
        self.assertEqual([item.name for item in overrides], ["handle"])
        self.assertTrue(overrides[0].path.endswith("Impl.java"))




class MultiLanguageIndexTests(unittest.TestCase):
    def test_python_inheritance_and_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "base.py").write_text(
                "class Base:\n"
                "    def handle(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            (root / "impl.py").write_text(
                "from base import Base\n"
                "class Impl(Base):\n"
                "    def handle(self):\n"
                "        self.helper()\n"
                "    def helper(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            index = RepoIndex(root, {"include": ["**/*.py"]})
            self.assertEqual([s.name for s in index.implementations("Base")], ["Impl"])
            base = next(s for s in index.symbols_named("handle") if s.path == "base.py")
            self.assertEqual([s.path for s in index.overrides(base)], ["impl.py"])


REACT_FIXTURE = BENCH_ROOT / "tests" / "fixtures" / "react"


class ReactIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = RepoIndex(
            REACT_FIXTURE,
            {"include": ["**/*.tsx", "**/*.ts", "**/*.jsx", "**/*.js"]},
        )

    def test_module_imports_resolve(self):
        self.assertEqual(
            self.index.importers_of("src/components/Button.tsx"),
            ["src/App.tsx"],
        )
        self.assertEqual(
            self.index.importers_of("src/components/Plain.jsx"),
            ["src/App.tsx"],
        )
        self.assertEqual(
            self.index.importers_of("src/hooks/useTheme.ts"),
            ["src/App.tsx", "src/components/Button.tsx"],
        )

    def test_commented_import_is_ignored(self):
        self.assertFalse(any("Nope" in path for path in self.index.files))
        targets = {
            target
            for resolved in self.index.imports.values()
            for target in resolved
        }
        self.assertFalse(any("Nope" in target for target in targets))

    def test_destructured_parameters_do_not_end_symbol(self):
        button = self.index.unique_symbol("Button")
        self.assertIsNotNone(button)
        self.assertGreater(button.end_line, button.line)
        icon = self.index.unique_symbol("IconButton")
        self.assertIsNotNone(icon)
        self.assertGreater(icon.end_line, icon.line)

    def test_jsx_renders_are_call_edges(self):
        button = self.index.unique_symbol("Button")
        icon = self.index.unique_symbol("IconButton")
        self.assertEqual(
            sorted(caller.name for caller in self.index.callers(button)),
            ["App", "IconButton"],
        )
        self.assertEqual(
            [caller.name for caller in self.index.callers(icon)],
            ["App"],
        )
        self.assertEqual(
            [callee.name for callee in self.index.callees(icon)],
            ["Button"],
        )
        plain = self.index.unique_symbol("Plain")
        self.assertEqual(
            [caller.name for caller in self.index.callers(plain)],
            ["App"],
        )
        edge_names = {name for _owner, name, _line in self.index.edges}
        self.assertNotIn("div", edge_names)
        self.assertNotIn("span", edge_names)

    def test_qualified_react_component_base_class(self):
        legacy = self.index.unique_symbol("LegacyPanel")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.kind, "class")
        self.assertIn(
            "LegacyPanel",
            [item.name for item in self.index.subtypes("Component")],
        )

    def test_hook_calls_inside_destructured_component(self):
        hook = self.index.unique_symbol("useTheme")
        self.assertIsNotNone(hook)
        self.assertEqual(
            sorted(caller.name for caller in self.index.callers(hook)),
            ["App", "Button"],
        )

    def test_arrow_parameter_types_with_nested_parens(self):
        symbol = self.index.unique_symbol("useAfter")
        self.assertIsNotNone(symbol)
        self.assertGreater(symbol.end_line, symbol.line)


class ParserTests(unittest.TestCase):
    def test_recursive_json_reads_end_line(self):
        payload = json.dumps([
            {"node": {"filePath": "a/B.java", "startLine": 5, "endLine": 9}, "score": 2}
        ])
        items = parse_recursive_json(payload, "/repo")
        self.assertEqual(items[0]["path"], "a/B.java")
        self.assertEqual(items[0]["line"], 5)
        self.assertEqual(items[0]["end_line"], 9)

    def test_cbm_groups(self):
        payload = json.dumps({
            "groups": [{
                "file": "src/A.java",
                "rows": [["foo", "Method", "10-12", 0, 1]],
            }]
        })
        items = parse_cbm(payload, "/repo")
        self.assertEqual(items[0]["path"], "src/A.java")
        self.assertEqual((items[0]["line"], items[0]["end_line"]), (10, 12))

    def test_codegraph_dependents_header(self):
        payload = (
            "**src/main/java/com/example/Owner.java** — 24 symbols, used by 2 files: "
            "src/main/java/com/example/OwnerResource.java, "
            "src/main/java/com/example/PetResource.java\n"
        )
        items = parse_codegraph_dependents(payload, "/repo")
        self.assertEqual(
            [item["path"] for item in items],
            [
                "src/main/java/com/example/OwnerResource.java",
                "src/main/java/com/example/PetResource.java",
            ],
        )

    def test_cbm_semantic_table(self):
        payload = json.dumps({
            "semantic": {
                "cols": ["qn", "label", "file", "score"],
                "rows": [["Pkg.Foo", "Method", "src/Foo.java", 0.42]],
            },
            "groups": [],
        })
        items = parse_cbm_semantic(payload, "/repo")
        self.assertEqual(items[0]["path"], "src/Foo.java")
        self.assertEqual(items[0]["symbol"], "Pkg.Foo")
        self.assertEqual(items[0]["score"], 0.42)

    def test_codegraph_relation_parsers_exclude_target(self):
        payload = json.dumps({
            "symbol": "target",
            "targets": [{"name": "target", "filePath": "src/A.java", "startLine": 10}],
            "definitions": [{
                "definition": {
                    "name": "target", "filePath": "src/A.java", "startLine": 10,
                },
                "roots": ["method:target"],
                "callers": [{
                    "name": "caller", "filePath": "src/B.java", "startLine": 20,
                }],
                "callees": [{
                    "name": "callee", "filePath": "src/C.java", "startLine": 30,
                }],
                "affected": [
                    {"id": "method:target", "name": "target",
                     "filePath": "src/A.java", "startLine": 10},
                    {"id": "method:caller", "name": "caller",
                     "filePath": "src/B.java", "startLine": 20},
                ],
            }],
        })
        callers = parse_codegraph_callers(payload, "/repo")
        self.assertEqual([(i["path"], i["symbol"]) for i in callers],
                         [("src/B.java", "caller")])
        callees = parse_codegraph_callees(payload, "/repo")
        self.assertEqual([(i["path"], i["symbol"]) for i in callees],
                         [("src/C.java", "callee")])
        affected = parse_codegraph_affected(payload, "/repo")
        self.assertEqual([(i["path"], i["symbol"]) for i in affected],
                         [("src/B.java", "caller")])

    def test_cbm_query_table(self):
        payload = json.dumps({
            "columns": ["a.name", "a.file_path", "b.name"],
            "rows": [["caller", "src/A.java", "target"]],
        })
        items = parse_cbm_query(payload, "/repo")
        self.assertEqual(items[0]["path"], "src/A.java")
        self.assertEqual(items[0]["symbol"], "caller")


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        for name in ("A.java", "B.java", "C.java"):
            (self.root / "src" / name).write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_localization(self):
        instance = {
            "gold": {"files": ["src/A.java"], "hunks": {"src/A.java": [[5, 8]]}}
        }
        items = [
            {"path": "src/B.java", "line": 1, "end_line": 1},
            {"path": "src/A.java", "line": 7, "end_line": 7},
        ]
        metrics = score_localization(items, instance, 10, self.root)
        self.assertEqual(metrics["rank"], 2)
        self.assertTrue(metrics["hunk@5"])
        self.assertAlmostEqual(metrics["mrr@10"], 0.5)

    def test_site(self):
        instance = {
            "gold": {"sites": [{"path": "src/A.java", "line": 10, "end_line": 12}]}
        }
        items = [
            {"path": "src/A.java", "line": 1, "end_line": 2},
            {"path": "src/A.java", "line": 11, "end_line": 11},
        ]
        metrics = score_site(items, instance, 10, self.root)
        self.assertTrue(metrics["hit@1"] is False)
        self.assertEqual(metrics["rank"], 2)

    def test_aggregate_preserves_fractional_recall(self):
        rows = [{
            "error": None,
            "prepare_s": 0.1,
            "query_s": 0.1,
            "metrics": {
                "recall@5": 1 / 3,
                "precision@10": 0.5,
                "hit@10": True,
                "files_returned": 4,
                "context_lines": 4,
                "invalid_paths": 0,
            },
        }]
        stat = aggregate_rows(
            rows, {"metrics": ["recall@5", "precision@10", "hit@10"]}, 10
        )
        self.assertAlmostEqual(stat["metrics"]["recall@5"], 100 / 3)
        self.assertAlmostEqual(stat["metrics"]["precision@10"], 50.0)
        self.assertEqual(stat["metrics"]["hit@10"], 100.0)

    def test_aggregate_percentiles(self):
        rows = [{
            "error": None,
            "prepare_s": 0.2,
            "query_s": 0.3,
            "query_s_samples": [0.1, 0.3, 0.5],
            "metrics": {
                "recall@5": 1.0,
                "files_returned": 1,
                "context_lines": 1,
                "invalid_paths": 0,
            },
        }]
        stat = aggregate_rows(rows, {"metrics": ["recall@5"]}, 10)
        self.assertAlmostEqual(stat["query_s"], 0.3)
        self.assertAlmostEqual(stat["query_s_p95"], 0.5)
        self.assertEqual(stat["query_samples"], 3)

    def test_set_recall(self):
        instance = {"gold": {"files": ["src/A.java", "src/B.java"]}}
        items = [
            {"path": "src/C.java"},
            {"path": "src/A.java"},
        ]
        metrics = score_set(items, instance, 10, self.root)
        self.assertAlmostEqual(metrics["recall@5"], 0.5)
        self.assertFalse(metrics["all@10"])
        self.assertTrue(metrics["hit@5"])

    def test_set_site_and_edge_metrics(self):
        instance = {
            "task": "callers",
            "query": {"symbol": "target"},
            "gold": {
                "files": ["src/A.java", "src/B.java"],
                "sites": [
                    {"path": "src/A.java", "line": 10, "name": "callerA"},
                    {"path": "src/B.java", "line": 20, "name": "callerB"},
                ],
                "edges": [
                    {"path": "src/A.java", "from": "callerA", "to": "target"},
                    {"path": "src/B.java", "from": "callerB", "to": "target"},
                ],
            },
        }
        items = [
            {"path": "src/A.java", "line": 10, "end_line": 10, "symbol": "callerA"},
            {"path": "src/B.java", "line": 20, "end_line": 20, "symbol": "wrong"},
        ]
        metrics = score_set(items, instance, 10, self.root)
        self.assertAlmostEqual(metrics["site_recall@10"], 1.0)
        self.assertAlmostEqual(metrics["site_precision@10"], 1.0)
        self.assertAlmostEqual(metrics["edge_recall@10"], 0.5)
        self.assertAlmostEqual(metrics["edge_precision@10"], 0.5)
        self.assertAlmostEqual(metrics["edge_f1@10"], 0.5)

    def test_absence(self):
        instance = {"gold": {"files": []}}
        clean = score_absence([], instance, 10, self.root)
        noisy = score_absence([{"path": "src/A.java"}], instance, 10, self.root)
        self.assertTrue(clean["abstain@10"])
        self.assertFalse(noisy["abstain@10"])
        self.assertEqual(noisy["false_positives@10"], 1)

    def test_sites_scorer(self):
        instance = {
            "gold": {
                "sites": [
                    {"path": "src/A.java", "line": 10, "name": "Foo"},
                    {"path": "src/B.java", "line": 20, "name": "Foo"},
                ]
            }
        }
        items = [
            {"path": "src/A.java", "line": 10, "end_line": 10, "symbol": "Foo"},
            {"path": "src/C.java", "line": 1, "end_line": 1, "symbol": "Foo"},
        ]
        metrics = score_sites(items, instance, 10, self.root)
        self.assertAlmostEqual(metrics["site_recall@10"], 0.5)
        self.assertFalse(metrics["all_sites@10"])


class ScopeAndSplitTests(unittest.TestCase):
    def test_scope_filter(self):
        cfg = {"include": ["**/*.java"], "exclude": ["**/src/test/**"]}
        self.assertTrue(path_in_scope("src/main/java/A.java", cfg))
        self.assertFalse(path_in_scope("src/test/java/A.java", cfg))
        self.assertFalse(path_in_scope("README.md", cfg))

    def test_assign_splits_is_deterministic_and_preserves_existing(self):
        instances = [
            {"instance_id": "a", "meta": {}},
            {"instance_id": "b", "meta": {"split": "test"}},
        ]
        first = assign_splits([dict(item, meta=dict(item["meta"])) for item in instances], 1.0, 0)
        second = assign_splits([dict(item, meta=dict(item["meta"])) for item in instances], 1.0, 0)
        self.assertEqual(
            [item["meta"]["split"] for item in first],
            [item["meta"]["split"] for item in second],
        )
        self.assertEqual(first[1]["meta"]["split"], "test")


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = BenchHome(Path(self.temp.name))
        self.home.ensure_dirs()
        save_json(self.home.repos_path(), {"version": 1, "repos": {}})
        save_json(self.home.tools_path(), {"version": 1, "tools": {}})
        save_json(self.home.tasks_path(), {"version": 1, "tasks": {}})
        self.repo = Path(self.temp.name) / "sample"
        (self.repo / ".git").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_add_and_remove_repo(self):
        args = Namespace(
            action="add", name="sample", path=str(self.repo), language="java",
            include=None, exclude=None, slug=None, disabled=False, force=False,
            allow_missing=False,
        )
        cmd_repos(self.home, args)
        repos = load_json(self.home.repos_path())["repos"]
        self.assertIn("sample", repos)
        args.action = "remove"
        cmd_repos(self.home, args)
        self.assertNotIn("sample", load_json(self.home.repos_path())["repos"])

    def test_add_and_validate_tool(self):
        tool_file = Path(self.temp.name) / "tool.json"
        save_json(tool_file, {
            "name": "fake",
            "kind": "fake",
            "queries": {"localization": {"argv": ["echo", "{}"]}},
            "parse": "recursive_json",
        })
        args = Namespace(
            action="add", name=None, file=str(tool_file), force=False,
        )
        cmd_tools(self.home, args)
        tools = load_json(self.home.tools_path())["tools"]
        self.assertIn("fake", tools)
        self.assertIsNotNone(
            query_for_task(tools["fake"], "localization", {"allow_default_query": True})
        )


class EndToEndTests(unittest.TestCase):
    def _git(self, repo, *args, env=None):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return result.stdout

    def test_offline_pipeline(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        home_dir = base / "home"
        repo = base / "repo"
        (repo / "src/main/java/com/example").mkdir(parents=True)

        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        self._git(repo, "init", "-q")
        self._git(repo, "config", "commit.gpgsign", "false")
        self._git(repo, "config", "tag.gpgsign", "false")
        (repo / "src/main/java/com/example/Foo.java").write_text(
            "package com.example;\n"
            "public class Foo {\n"
            "    public int value() { return 1; }\n"
            "}\n",
            encoding="utf-8",
        )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-qm", "base", env=env)
        (repo / "src/main/java/com/example/Foo.java").write_text(
            "package com.example;\n"
            "public class Foo {\n"
            "    public int value() { return 2; }\n"
            "}\n",
            encoding="utf-8",
        )
        message = (
            "Fix value computation #1\n\n"
            "This issue explains that the value method returns the wrong number "
            "for the sample application, and it should be updated to return two "
            "so downstream consumers observe correct behaviour in production."
        )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-qm", message, env=env)

        env["BENCH_HOME"] = str(home_dir)
        def bench(*args):
            return subprocess.run(
                [sys.executable, str(BENCH_ROOT / "bench.py"), *args],
                capture_output=True, text=True, env=env,
            )

        self.assertEqual(bench("init").returncode, 0)
        repos_payload = load_json(home_dir / "config/repos.json")
        repos_payload["repos"] = {
            "sample": {
                "enabled": True,
                "path": str(repo),
                "language": "java",
                "include": ["**/*.java"],
                "exclude": ["**/src/test/**"],
            }
        }
        save_json(home_dir / "config/repos.json", repos_payload)

        fake = base / "fake_tool.py"
        fake.write_text(
            "import json, sys\n"
            "print(json.dumps({'results': [{'file_path': "
            "'src/main/java/com/example/Foo.java', 'start_line': 1, "
            "'end_line': 4}]}))\n",
            encoding="utf-8",
        )
        tools_payload = load_json(home_dir / "config/tools.json")
        tools_payload["tools"]["fake"] = {
            "enabled": True,
            "kind": "fake",
            "queries": {
                "localization": {
                    "argv": [sys.executable, str(fake)],
                }
            },
            "parse": "recursive_json",
        }
        save_json(home_dir / "config/tools.json", tools_payload)

        mined = bench(
            "mine", "--repo", "sample", "--tasks", "localization",
            "--limit", "1", "--offline", "--scan", "20",
            "--min-problem-chars", "50",
        )
        self.assertEqual(mined.returncode, 0, mined.stderr)

        run = bench(
            "run", "--tasks", "localization", "--tools", "fake",
            "--run-id", "e2e", "--no-report",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = [
            json.loads(line)
            for line in (home_dir / "results/e2e/results.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["error"])
        self.assertTrue(rows[0]["metrics"]["recall@1"])

        report = bench("report", "--run", "e2e")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertTrue((home_dir / "results/e2e/summary.md").exists())
        self.assertTrue((home_dir / "results/e2e/summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
