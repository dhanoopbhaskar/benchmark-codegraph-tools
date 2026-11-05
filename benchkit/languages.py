"""Lightweight, dependency-free symbol / call / import indexing.

This is deliberately heuristic. It is not a compiler: it uses masked source
(comments and strings blanked out), regex declaration matching, brace/indent
range detection and simple import resolution. That is enough to derive
deterministic ground truth for structural benchmark tasks without an LLM or a
language server, and it works the same on every platform.

Supported languages: Java, Python, JavaScript/TypeScript, Go (basic). Other
files are ignored for structural mining.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".codegraph", ".context", ".codebase-memory",
    "node_modules", "target", "build", "dist", "out", "vendor", "Pods",
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".idea", ".gradle", "bin", "obj", "coverage", "site-packages",
}

LANGUAGE_BY_SUFFIX = {
    ".java": "java",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
}

CONTROL_WORDS = {
    "if", "else", "for", "while", "switch", "case", "catch", "try", "finally",
    "return", "throw", "throws", "new", "typeof", "sizeof", "instanceof",
    "super", "this", "self", "assert", "yield", "await", "delete", "do",
    "with", "function", "class", "interface", "enum", "record", "def", "lambda",
    "using", "lock", "foreach", "synchronized", "static", "public", "private",
    "protected", "final", "abstract", "native", "default", "package", "import",
    "from", "as", "in", "is", "not", "and", "or", "match", "when", "unless",
}

EXCLUDED_SYMBOL_NAMES = {
    "main", "run", "init", "get", "set", "of", "apply", "builder", "toString",
    "equals", "hashCode", "compareTo", "clone", "finalize", "close", "open",
    "read", "write", "test", "setUp", "tearDown",
}


@dataclass
class Symbol:
    name: str
    kind: str              # method/function/constructor/class/interface/enum/record/struct
    path: str              # POSIX path relative to repo root
    line: int              # 1-based declaration line
    end_line: int          # 1-based inclusive end line
    has_body: bool
    language: str

    @property
    def is_callable(self) -> bool:
        return self.kind in {"method", "function", "constructor"} and self.has_body

    @property
    def is_type(self) -> bool:
        return self.kind in {"class", "interface", "enum", "record", "struct"}

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "has_body": self.has_body,
            "language": self.language,
        }

    def key(self) -> tuple:
        return (self.path, self.line, self.name, self.kind)


def detect_language(path: str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())


def glob_match(path: str, pattern: str) -> bool:
    """Match a repo-relative POSIX path against a glob, including ``**`` roots."""
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    variants = {pattern}
    if pattern.startswith("**/"):
        variants.add(pattern[3:])
    if pattern.endswith("/**"):
        variants.add(pattern[:-3])
    if pattern.endswith("/**/*"):
        variants.add(pattern[:-5])
    return any(fnmatch.fnmatchcase(path, candidate) for candidate in variants)



def _blank_span(chars: list[str], start: int, end: int) -> None:
    for index in range(start, min(end, len(chars))):
        if chars[index] != "\n":
            chars[index] = " "


def mask_code(text: str, language: str | None) -> str:
    """Replace comments and string literals with spaces, preserving newlines.

    A single-pass scanner is used instead of regexes because Java route strings
    such as ``"/owners/*/pets/{petId}"`` contain character pairs that look like
    comment delimiters.
    """
    chars = list(text)
    length = len(text)
    index = 0
    python = language == "python"
    javascript = language in {"javascript", "typescript"}
    while index < length:
        char = text[index]

        if python and char == "#":
            end = text.find("\n", index)
            end = length if end < 0 else end
            _blank_span(chars, index, end)
            index = end
            continue

        if not python and text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            _blank_span(chars, index, end)
            index = end
            continue

        if not python and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            _blank_span(chars, index, end)
            index = end
            continue

        triple = False
        quote = ""
        if char in {"\"", "'"}:
            quote = char
            if text.startswith(char * 3, index) and (python or language == "java"):
                quote = char * 3
                triple = True
        elif javascript and char == "`":
            quote = "`"

        if quote:
            cursor = index + len(quote)
            while cursor < length:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text.startswith(quote, cursor):
                    cursor += len(quote)
                    break
                if not triple and quote != "`" and text[cursor] == "\n":
                    break
                cursor += 1
            _blank_span(chars, index, cursor)
            index = cursor
            continue

        index += 1
    return "".join(chars)


def _line_at(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def _brace_end_line(masked: str, offsets: list[int], start_line: int) -> tuple[int, bool]:
    """Find the matching ``}`` for the first ``{`` on/after *start_line*."""
    start_index = offsets[max(0, min(start_line - 1, len(offsets) - 1))]
    open_index = masked.find("{", start_index)
    if open_index < 0:
        return start_line, False
    # A semicolon before the opening brace means an abstract/interface method.
    head = masked[start_index:open_index]
    if ";" in head:
        return start_line, False
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _line_at(masked, index), True
    return max(start_line, masked.count("\n") + 1), True


def _indent_end_line(lines: list[str], start_line: int, indent: int) -> int:
    last = start_line
    for index in range(start_line, len(lines)):
        raw = lines[index]
        if not raw.strip():
            continue
        current = len(raw) - len(raw.lstrip(" \t"))
        if current <= indent:
            break
        last = index + 1
    return last


def _add_symbol(
    bucket: list[Symbol],
    seen: set[tuple],
    name: str,
    kind: str,
    path: str,
    line: int,
    end_line: int,
    has_body: bool,
    language: str,
) -> None:
    if not name or line < 1:
        return
    symbol = Symbol(name, kind, path, line, max(line, end_line), has_body, language)
    if symbol.key() in seen:
        return
    seen.add(symbol.key())
    bucket.append(symbol)


JAVA_TYPE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
JAVA_METHOD = re.compile(
    r"(?m)^[ \t]*(?:(?:@[\w$.]+(?:\([^)\n]*\))?\s*)*)"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?:<[^>\n]+>\s*)?"
    r"([A-Za-z_$][\w$<>\[\].,?]*)\s+([A-Za-z_$][\w$]*)\s*\("
)
JAVA_CTOR = re.compile(
    r"(?m)^[ \t]*(?:(?:@[\w$.]+(?:\([^)\n]*\))?\s*)*)"
    r"(?:(?:public|protected|private)\s+)*"
    r"([A-Z][A-Za-z0-9_$]*)\s*\("
)
PY_DEF = re.compile(r"(?m)^([ \t]*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
PY_CLASS = re.compile(r"(?m)^([ \t]*)class\s+([A-Za-z_]\w*)")
JS_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
JS_CLASS = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)\b")
JS_ARROW = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\([^)\n]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
JS_METHOD = re.compile(
    r"(?m)^[ \t]*(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?"
    r"([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*\{"
)
GO_FUNC = re.compile(r"(?m)^[ \t]*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")

CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

JAVA_IMPORT = re.compile(r"(?m)^[ \t]*import\s+(?:static\s+)?([\w.$*]+)\s*;")
PY_IMPORT = re.compile(r"(?m)^[ \t]*import\s+([^\n#]+)")
PY_FROM = re.compile(r"(?m)^[ \t]*from\s+([.\w]*)\s+import\s+")
JS_IMPORT = re.compile(
    r"(?:\bfrom\s+['\"]([^'\"]+)['\"]"
    r"|\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\))"
)


def parse_file(path: str, text: str, language: str) -> tuple[list[Symbol], list[str]]:
    """Return ``(symbols, import_specs)`` for one source file."""
    masked = mask_code(text, language)
    lines = masked.splitlines()
    offsets = _line_offsets(masked)
    symbols: list[Symbol] = []
    seen: set[tuple] = set()
    imports: list[str] = []

    if language == "java":
        types: dict[str, int] = {}
        for match in JAVA_TYPE.finditer(masked):
            kind, name = match.group(1), match.group(2)
            line = _line_at(masked, match.start())
            end_line, has_body = _brace_end_line(masked, offsets, line)
            types.setdefault(name, line)
            _add_symbol(symbols, seen, name, kind, path, line, end_line, has_body, language)

        for match in JAVA_METHOD.finditer(masked):
            return_type, name = match.group(1), match.group(2)
            if return_type in CONTROL_WORDS or name in CONTROL_WORDS:
                continue
            line = _line_at(masked, match.start(2))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "method", path, line, end_line, has_body, language)

        for match in JAVA_CTOR.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            if name not in types or types[name] != line:
                continue
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "constructor", path, line, end_line, has_body, language)

        imports = [m.group(1) for m in JAVA_IMPORT.finditer(masked)]

    elif language == "python":
        for match in PY_DEF.finditer(masked):
            indent = len(match.group(1).expandtabs(4))
            name = match.group(2)
            line = _line_at(masked, match.start(2))
            end_line = _indent_end_line(lines, line, indent)
            _add_symbol(symbols, seen, name, "function", path, line, end_line, True, language)

        for match in PY_CLASS.finditer(masked):
            indent = len(match.group(1).expandtabs(4))
            name = match.group(2)
            line = _line_at(masked, match.start(2))
            end_line = _indent_end_line(lines, line, indent)
            _add_symbol(symbols, seen, name, "class", path, line, end_line, True, language)

        for match in PY_IMPORT.finditer(masked):
            for part in match.group(1).split(","):
                spec = part.strip().split(" as ")[0].strip()
                if spec:
                    imports.append(spec)
        for match in PY_FROM.finditer(masked):
            imports.append(match.group(1) or ".")

    elif language in {"javascript", "typescript"}:
        for match in JS_CLASS.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "class", path, line, end_line, has_body, language)
        for match in JS_FUNCTION.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "function", path, line, end_line, has_body, language)
        for match in JS_ARROW.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "function", path, line, end_line, has_body, language)
        for match in JS_METHOD.finditer(masked):
            name = match.group(1)
            if name in CONTROL_WORDS:
                continue
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "method", path, line, end_line, has_body, language)
        for match in JS_IMPORT.finditer(masked):
            spec = match.group(1) or match.group(2) or match.group(3)
            if spec:
                imports.append(spec)

    elif language == "go":
        for match in GO_FUNC.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "function", path, line, end_line, has_body, language)
        for match in re.finditer(r"(?m)^[ \t]*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _brace_end_line(masked, offsets, line)
            _add_symbol(symbols, seen, name, "struct", path, line, end_line, has_body, language)

    symbols.sort(key=lambda item: (item.line, item.name))
    return symbols, imports


def _java_fqn(path: str, name: str) -> str | None:
    """Derive ``package.Class`` from a conventional Java source path."""
    parts = path.replace("\\", "/").split("/")
    if "java" not in parts:
        return name
    index = len(parts) - 1 - parts[::-1].index("java")
    package_parts = parts[index + 1 : -1]
    if not package_parts:
        return name
    return ".".join(package_parts + [name])


class RepoIndex:
    """Index of symbols, call sites and imports for one checked-out tree."""

    def __init__(self, root, repo_cfg: dict):
        self.root = Path(root)
        self.repo_cfg = repo_cfg or {}
        self.includes = list(self.repo_cfg.get("include") or [])
        self.excludes = list(self.repo_cfg.get("exclude") or [])
        self.max_bytes = int(self.repo_cfg.get("max_file_bytes", 1_500_000))
        self.files: list[str] = []
        self.symbols: list[Symbol] = []
        self.by_name: dict[str, list[Symbol]] = {}
        self.by_file: dict[str, list[Symbol]] = {}
        self.import_specs: dict[str, list[str]] = {}
        self.imports: dict[str, set[str]] = {}
        self.importers: dict[str, set[str]] = {}
        self.type_to_paths: dict[str, set[str]] = {}
        self.java_fqn_to_paths: dict[str, set[str]] = {}
        self.module_to_path: dict[str, str] = {}
        self.edges: list[tuple[Symbol, str, int]] = []
        self.calls_by_name: dict[str, list[Symbol]] = {}
        self._parse()

    # ------------------------------------------------------------------ scan
    def should_include(self, rel_path: str) -> bool:
        if any(glob_match(rel_path, pattern) for pattern in self.excludes):
            return False
        if self.includes and not any(glob_match(rel_path, pattern) for pattern in self.includes):
            return False
        return False if detect_language(rel_path) is None else True

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in SKIP_DIRS and not name.startswith(".")
            )
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                try:
                    rel = full.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if not self.should_include(rel):
                    continue
                try:
                    if full.stat().st_size > self.max_bytes:
                        continue
                except OSError:
                    continue
                yield full, rel

    def _parse(self) -> None:
        for full, rel in self._iter_files():
            language = detect_language(rel)
            if language is None:
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            try:
                symbols, imports = parse_file(rel, text, language)
            except Exception:
                continue
            self.files.append(rel)
            self.import_specs[rel] = imports
            for symbol in symbols:
                self.symbols.append(symbol)
                self.by_name.setdefault(symbol.name, []).append(symbol)
                self.by_file.setdefault(symbol.path, []).append(symbol)
                if symbol.is_type:
                    self.type_to_paths.setdefault(symbol.name, set()).add(symbol.path)
                    if symbol.language == "java":
                        fqn = _java_fqn(symbol.path, symbol.name)
                        if fqn:
                            self.java_fqn_to_paths.setdefault(fqn, set()).add(symbol.path)
            if rel.endswith(".py"):
                module = rel[:-3].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[: -len(".__init__")]
                self.module_to_path[module] = rel

        self._build_call_edges()
        self._build_import_graph()

    # --------------------------------------------------------------- symbols
    def symbols_in_file(self, path: str) -> list[Symbol]:
        return list(self.by_file.get(path, []))

    def unique_symbol(self, name: str) -> Symbol | None:
        symbols = self.by_name.get(name, [])
        unique = {(symbol.path, symbol.line): symbol for symbol in symbols}
        if len(unique) != 1:
            return None
        return next(iter(unique.values()))

    # ----------------------------------------------------------------- calls
    def _build_call_edges(self) -> None:
        for path in self.files:
            callables = [s for s in self.by_file.get(path, []) if s.is_callable]
            if not callables:
                continue
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            language = detect_language(path)
            masked = mask_code(text, language)
            offsets = _line_offsets(masked)
            decl_lines = {s.line for s in self.by_file.get(path, [])}
            for match in CALL_RE.finditer(masked):
                name = match.group(1)
                if name in CONTROL_WORDS:
                    continue
                start = match.start(1)
                if start > 0 and masked[start - 1] == "@":
                    continue
                line = _line_at(masked, start)
                if line in decl_lines:
                    continue
                owner = None
                for symbol in callables:
                    if symbol.line <= line <= symbol.end_line:
                        if owner is None or symbol.line > owner.line:
                            owner = symbol
                if owner is None:
                    continue
                self.edges.append((owner, name, line))

        for owner, name, _line in self.edges:
            self.calls_by_name.setdefault(name, []).append(owner)

    def callers(self, target: Symbol) -> list[Symbol]:
        """Caller symbols whose body contains a call to *target*'s name."""
        unique_target = self.unique_symbol(target.name)
        if unique_target is None:
            return []
        output: dict[tuple, Symbol] = {}
        for caller in self.calls_by_name.get(target.name, []):
            if caller.key() == unique_target.key():
                continue
            output[caller.key()] = caller
        return list(output.values())

    def callees(self, target: Symbol) -> list[Symbol]:
        """Definitions of symbols called from *target*'s body."""
        output: dict[tuple, Symbol] = {}
        for owner, name, _line in self.edges:
            if owner.key() != target.key():
                continue
            resolved = self.unique_symbol(name)
            if resolved is None or resolved.key() == target.key():
                continue
            output[resolved.key()] = resolved
        return list(output.values())

    def impact(self, target: Symbol, depth: int = 2) -> list[Symbol]:
        """Transitive callers of *target* up to *depth* hops."""
        seen: dict[tuple, Symbol] = {}
        frontier = [target]
        for _ in range(max(1, depth)):
            next_frontier: list[Symbol] = []
            for current in frontier:
                for caller in self.callers(current):
                    if caller.key() in seen or caller.key() == target.key():
                        continue
                    seen[caller.key()] = caller
                    next_frontier.append(caller)
            if not next_frontier:
                break
            frontier = next_frontier
        return list(seen.values())

    # --------------------------------------------------------------- imports
    def _build_import_graph(self) -> None:
        for importer, specs in self.import_specs.items():
            targets: set[str] = set()
            language = detect_language(importer)
            for spec in specs:
                targets.update(self._resolve_import(importer, spec, language))
            targets = {target for target in targets if target in self.by_file or target in self.files}
            targets.discard(importer)
            if not targets:
                continue
            self.imports[importer] = targets
            for target in targets:
                self.importers.setdefault(target, set()).add(importer)

    def _resolve_import(self, importer: str, spec: str, language: str | None) -> set[str]:
        spec = (spec or "").strip()
        if not spec:
            return set()
        if language == "java":
            return self._resolve_java_import(spec)
        if language == "python":
            return self._resolve_python_import(importer, spec)
        if language in {"javascript", "typescript"}:
            return self._resolve_js_import(importer, spec)
        return set()

    def _resolve_java_import(self, spec: str) -> set[str]:
        if spec.endswith(".*"):
            package = spec[:-2]
            prefix = package + "."
            return {
                path
                for fqn, paths in self.java_fqn_to_paths.items()
                if fqn.startswith(prefix)
                for path in paths
            }
        parts = spec.split(".")
        # Exact FQN, then progressively shorter prefixes (static imports).
        for count in range(len(parts), 0, -1):
            fqn = ".".join(parts[:count])
            if fqn in self.java_fqn_to_paths:
                return set(self.java_fqn_to_paths[fqn])
        # Fallback only when the simple name is unambiguous in this repo.
        name = parts[-1]
        paths = self.type_to_paths.get(name, set())
        return set(paths) if len(paths) == 1 else set()

    def _resolve_python_import(self, importer: str, spec: str) -> set[str]:
        importer_module = importer[:-3].replace("/", ".")
        if importer_module.endswith(".__init__"):
            importer_module = importer_module[: -len(".__init__")]
        if spec.startswith("."):
            dots = len(spec) - len(spec.lstrip("."))
            remainder = spec[dots:]
            package_parts = importer_module.split(".")
            if not importer.endswith("__init__.py"):
                package_parts = package_parts[:-1]
            keep = max(0, len(package_parts) - (dots - 1))
            base = ".".join(package_parts[:keep])
            module = ".".join(part for part in (base, remainder) if part)
        else:
            module = spec
        candidates = {module}
        # ``from pkg.mod import Thing`` should resolve to pkg.mod too.
        parts = module.split(".")
        for count in range(len(parts), 0, -1):
            candidates.add(".".join(parts[:count]))
        return {
            self.module_to_path[candidate]
            for candidate in candidates
            if candidate in self.module_to_path
        }

    def _resolve_js_import(self, importer: str, spec: str) -> set[str]:
        if not spec.startswith("."):
            return set()
        base = (Path(importer).parent).as_posix()
        joined = spec
        if base not in {"", "."}:
            joined = base + "/" + spec
        parts: list[str] = []
        for part in joined.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        base_path = "/".join(parts)
        candidates = [base_path]
        for suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            candidates.append(base_path + suffix)
        for suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            candidates.append(base_path + "/index" + suffix)
        existing = set(self.files)
        return {candidate for candidate in candidates if candidate in existing}

    # ------------------------------------------------------------ convenience
    def importers_of(self, path: str) -> list[str]:
        return sorted(self.importers.get(path, set()))

    def dependencies_of(self, path: str) -> list[str]:
        return sorted(self.imports.get(path, set()))
