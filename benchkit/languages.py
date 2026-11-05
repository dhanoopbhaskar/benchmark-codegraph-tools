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
    super_types: tuple[str, ...] = ()

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
            "super_types": list(self.super_types),
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


def mask_code(text: str, language: str | None, mask_strings: bool = True) -> str:
    """Replace comments and (optionally) string literals with spaces.

    Newlines are preserved so line numbers stay stable. A single-pass scanner
    is used instead of regexes because Java route strings such as
    ``"/owners/*/pets/{petId}"`` contain character pairs that look like comment
    delimiters.

    ``mask_strings=False`` keeps string contents while still blanking comments.
    Import statements (JS/TS ``from '...'`` / ``require('...')``) need this:
    the specifier lives inside a string, so masking it hides every import.
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
            if mask_strings:
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


def _brace_end_at(masked: str, open_index: int) -> tuple[int, bool]:
    """Find the matching ``}`` for the ``{`` at *open_index*."""
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _line_at(masked, index), True
    return max(_line_at(masked, open_index), masked.count("\n") + 1), True


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
    return _brace_end_at(masked, open_index)


def _matching_index(
    masked: str, open_index: int, open_char: str, close_char: str
) -> int:
    """Return the index of the ``close_char`` matching *open_index*, or -1."""
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _expression_end_line(
    masked: str, start_index: int, base_line: int
) -> tuple[int, bool]:
    """Best-effort end line for an arrow function's expression body."""
    depth = 0
    index = start_index
    while index < len(masked):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char == ";" and depth == 0:
            return _line_at(masked, index), True
        index += 1

    # No semicolon (ASI style): stop before a line that is outdented relative
    # to the declaration and does not look like a continuation.
    lines = masked.splitlines()
    start_line = _line_at(masked, start_index)
    declaration = lines[base_line - 1] if base_line <= len(lines) else ""
    indent = len(declaration) - len(declaration.lstrip(" \t"))
    last = start_line
    for number in range(start_line + 1, len(lines) + 1):
        raw = lines[number - 1]
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        current = len(raw) - len(stripped)
        continuation = stripped.startswith(("</", ")", "]", "}", ".", "?", ":", "&&", "||"))
        if current <= indent and not continuation:
            break
        last = number
    return max(base_line, last), True


def _arrow_index(masked: str, name_index: int) -> int:
    """Find the ``=>`` operator for an arrow function, ignoring nested ones.

    A parameter type such as ``(callback: () => void) => {}`` contains an
    earlier ``=>``. Only a token at bracket depth zero is the function's own
    arrow.
    """
    start = masked.find("=", name_index)
    index = name_index if start < 0 else start + 1
    depth = 0
    while index < len(masked):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and masked.startswith("=>", index):
            return index
        index += 1
    return -1


def _js_body_range(
    masked: str, name_index: int, *, is_arrow: bool = False
) -> tuple[int, bool]:
    """Return ``(end_line, has_body)`` for a JS/TS declaration.

    Unlike :func:`_brace_end_line`, this skips destructured parameters and type
    annotations. React components are usually written as
    ``function Button({ label }) {}`` or ``const Button = ({ label }) => {}``;
    the first ``{`` belongs to the parameter list, not the body, so using it
    would end the symbol on its declaration line.
    """
    base_line = _line_at(masked, name_index)
    if is_arrow:
        arrow = _arrow_index(masked, name_index)
        if arrow < 0:
            return base_line, False
        cursor = arrow + 2
        while cursor < len(masked) and masked[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(masked) and masked[cursor] == "{":
            return _brace_end_at(masked, cursor)
        return _expression_end_line(masked, cursor, base_line)

    paren = masked.find("(", name_index)
    if paren < 0:
        return base_line, False
    close = _matching_index(masked, paren, "(", ")")
    if close < 0:
        return base_line, False
    brace = masked.find("{", close)
    semicolon = masked.find(";", close)
    if brace < 0 or (0 <= semicolon < brace):
        return base_line, False
    return _brace_end_at(masked, brace)


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


def _owner_at(callables: list[Symbol], line: int) -> Symbol | None:
    """Innermost callable whose range contains *line*."""
    owner = None
    for symbol in callables:
        if symbol.line <= line <= symbol.end_line:
            if owner is None or symbol.line > owner.line:
                owner = symbol
    return owner


def _clean_type_name(value: str) -> str:
    """Reduce ``pkg.Outer<T>.Inner`` to a comparable simple type name."""
    value = re.sub(r"<[^>]*>", "", value or "")
    value = value.strip().strip(".")
    if not value:
        return ""
    return re.split(r"[.\s]+", value)[-1].strip()


def _split_type_list(value: str | None) -> tuple[str, ...]:
    output: list[str] = []
    for part in (value or "").split(","):
        name = _clean_type_name(part)
        if name and name not in {"Object", "Any", "object"} and name not in output:
            output.append(name)
    return tuple(output)


def _java_super_types(masked: str, match: re.Match) -> tuple[str, ...]:
    """Extract ``extends``/``implements`` names from a Java type header."""
    brace = masked.find("{", match.end())
    semicolon = masked.find(";", match.end())
    stops = [value for value in (brace, semicolon) if value >= 0]
    stop = min(stops) + 1 if stops else match.end() + 400
    header = masked[match.end(): stop]
    names: list[str] = []
    for pattern in (JAVA_EXTENDS, JAVA_IMPLEMENTS):
        found = pattern.search(header)
        if found:
            names.extend(_split_type_list(found.group(1)))
    return tuple(dict.fromkeys(names))


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
    super_types: tuple[str, ...] = (),
) -> None:
    if not name or line < 1:
        return
    symbol = Symbol(
        name, kind, path, line, max(line, end_line), has_body, language,
        tuple(super_types),
    )
    if symbol.key() in seen:
        return
    seen.add(symbol.key())
    bucket.append(symbol)


JAVA_TYPE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
JAVA_EXTENDS = re.compile(
    r"\bextends\s+([\w.$<>,?\s]+?)(?=\bimplements\b|\bpermits\b|\{)"
)
JAVA_IMPLEMENTS = re.compile(
    r"\bimplements\s+([\w.$<>,?\s]+?)(?=\bpermits\b|\{)"
)
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
PY_CLASS = re.compile(
    r"(?m)^([ \t]*)class\s+([A-Za-z_]\w*)\s*(?:\(([^)\n]*)\))?\s*:"
)
JS_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
JS_CLASS = re.compile(
    r"\bclass\s+([A-Za-z_$][\w$]*)\s*"
    r"(?:extends\s+([A-Za-z_$][\w$.]*)(?:\s*<[^>{}\n]*>)?)?\s*"
    r"(?:implements\s+([A-Za-z_$][\w$.,\s<>]*?))?\s*\{"
)
JS_ARROW = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\([^;\n]*?\)|[A-Za-z_$][\w$]*)\s*=>"
)
JS_METHOD = re.compile(
    r"(?m)^[ \t]*(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?"
    r"([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*\{"
)
GO_FUNC = re.compile(r"(?m)^[ \t]*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")

CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
# Custom JSX elements only: lowercase tags are host elements, not symbols.
JSX_OPEN = re.compile(r"<([A-Z][\w$]*(?:\.[A-Za-z_$][\w$]*)*)(?=[\s/>])")
JSX_SUFFIXES = (".js", ".jsx", ".tsx")

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
            _add_symbol(
                symbols, seen, name, kind, path, line, end_line, has_body,
                language, _java_super_types(masked, match),
            )

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
            _add_symbol(
                symbols, seen, name, "class", path, line, end_line, True,
                language, _split_type_list(match.group(3)),
            )

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
            supers = _split_type_list(match.group(2)) + _split_type_list(match.group(3))
            _add_symbol(
                symbols, seen, name, "class", path, line, end_line, has_body,
                language, tuple(dict.fromkeys(supers)),
            )
        for match in JS_FUNCTION.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _js_body_range(masked, match.start(1))
            _add_symbol(symbols, seen, name, "function", path, line, end_line, has_body, language)
        for match in JS_ARROW.finditer(masked):
            name = match.group(1)
            line = _line_at(masked, match.start(1))
            end_line, has_body = _js_body_range(
                masked, match.start(1), is_arrow=True
            )
            _add_symbol(symbols, seen, name, "function", path, line, end_line, has_body, language)
        for match in JS_METHOD.finditer(masked):
            name = match.group(1)
            if name in CONTROL_WORDS:
                continue
            line = _line_at(masked, match.start(1))
            end_line, has_body = _js_body_range(masked, match.start(1))
            _add_symbol(symbols, seen, name, "method", path, line, end_line, has_body, language)
        # Imports keep their string literals: the specifier is the thing we
        # need, and normal masking blanks it.
        import_view = mask_code(text, language, mask_strings=False)
        for match in JS_IMPORT.finditer(import_view):
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
        self.subtypes_by_name: dict[str, list[Symbol]] = {}
        self.container_by_symbol: dict[tuple, Symbol] = {}
        self._name_counts: dict[str, int] | None = None
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
        self._build_type_hierarchy()

    # --------------------------------------------------------------- symbols
    def symbols_in_file(self, path: str) -> list[Symbol]:
        return list(self.by_file.get(path, []))

    def unique_symbol(self, name: str) -> Symbol | None:
        symbols = self.by_name.get(name, [])
        unique = {(symbol.path, symbol.line): symbol for symbol in symbols}
        if len(unique) != 1:
            return None
        return next(iter(unique.values()))

    def symbols_named(self, name: str) -> list[Symbol]:
        unique = {(symbol.path, symbol.line): symbol for symbol in self.by_name.get(name, [])}
        return sorted(unique.values(), key=lambda item: (item.path, item.line))

    def name_counts(self) -> dict[str, int]:
        """Lazily count masked identifier occurrences across the whole tree."""
        if self._name_counts is not None:
            return self._name_counts
        counts: dict[str, int] = {}
        pattern = re.compile(r"\b[A-Za-z_$][\w$]*\b")
        for path in self.files:
            try:
                text = (self.root / path).read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                continue
            for match in pattern.finditer(mask_code(text, detect_language(path))):
                name = match.group(0)
                counts[name] = counts.get(name, 0) + 1
        self._name_counts = counts
        return counts

    def occurrence_count(self, name: str) -> int:
        return self.name_counts().get(name, 0)

    def _build_type_hierarchy(self) -> None:
        # Resolve the innermost type that contains each declaration. This gives
        # methods and constructors a class/interface identity without a compiler.
        for path, symbols in self.by_file.items():
            types = [symbol for symbol in symbols if symbol.is_type]
            for symbol in symbols:
                containers = [
                    parent for parent in types
                    if parent.line <= symbol.line <= parent.end_line
                    and parent.key() != symbol.key()
                ]
                if containers:
                    self.container_by_symbol[symbol.key()] = max(
                        containers, key=lambda item: item.line
                    )
            for symbol in types:
                for parent_name in symbol.super_types:
                    self.subtypes_by_name.setdefault(parent_name, []).append(symbol)

    def container_of(self, symbol: Symbol) -> Symbol | None:
        return self.container_by_symbol.get(symbol.key())

    def subtypes(self, type_name: str, transitive: bool = True) -> list[Symbol]:
        """Types that extend or implement *type_name* (direct or transitive)."""
        output: dict[tuple, Symbol] = {}
        frontier = [type_name]
        seen_names = {type_name}
        while frontier:
            current = frontier.pop(0)
            for child in self.subtypes_by_name.get(current, []):
                if child.key() in output:
                    continue
                output[child.key()] = child
                if transitive and child.name not in seen_names:
                    seen_names.add(child.name)
                    frontier.append(child.name)
        return sorted(output.values(), key=lambda item: (item.path, item.line))

    def implementations(self, type_name: str) -> list[Symbol]:
        """Alias for subtypes; useful for interface/implementation queries."""
        return self.subtypes(type_name)

    def ambiguous_symbols(self) -> list[Symbol]:
        """One representative for each name declared in more than one place."""
        output: dict[str, Symbol] = {}
        for name, symbols in self.by_name.items():
            unique = {(item.path, item.line): item for item in symbols}
            if len(unique) > 1 and name not in output:
                output[name] = next(iter(unique.values()))
        return list(output.values())

    def parent_types(self) -> list[Symbol]:
        """Types that actually have at least one known subtype."""
        output: dict[str, Symbol] = {}
        for name in self.subtypes_by_name:
            if name not in output:
                symbol = self.unique_symbol(name)
                if symbol is not None and symbol.is_type:
                    output[name] = symbol
        return list(output.values())

    def overridable_methods(self) -> list[Symbol]:
        """Methods whose containing type has at least one subtype."""
        parent_names = set(self.subtypes_by_name)
        output: dict[tuple, Symbol] = {}
        for symbol in self.symbols:
            if not symbol.is_callable:
                continue
            container = self.container_of(symbol)
            if container is None or container.name not in parent_names:
                continue
            if self.overrides(symbol):
                output[symbol.key()] = symbol
        return list(output.values())

    def methods_in_type(self, type_symbol: Symbol) -> list[Symbol]:
        return [
            symbol for symbol in self.by_file.get(type_symbol.path, [])
            if symbol.is_callable
            and type_symbol.line <= symbol.line <= type_symbol.end_line
        ]

    def overrides(self, method: Symbol) -> list[Symbol]:
        """Methods in subtypes with the same name as *method*."""
        container = self.container_of(method)
        if container is None:
            return []
        output: dict[tuple, Symbol] = {}
        for subtype in self.subtypes(container.name):
            for candidate in self.methods_in_type(subtype):
                if candidate.name == method.name and candidate.key() != method.key():
                    output[candidate.key()] = candidate
        return sorted(output.values(), key=lambda item: (item.path, item.line))

    # ------------------------------------------------------------ references
    def references(self, target: Symbol) -> list[tuple[Symbol | None, int, str]]:
        """All non-declaration occurrences of *target*'s name.

        Returns ``(owning callable or None, line, path)`` tuples. Comments and
        string literals are already masked by :func:`mask_code`.
        """
        pattern = re.compile(r"\b" + re.escape(target.name) + r"\b")
        declaration_lines = {
            (symbol.path, symbol.line)
            for symbol in self.symbols_named(target.name)
        }
        output: list[tuple[Symbol | None, int, str]] = []
        for path in self.files:
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            masked = mask_code(text, detect_language(path))
            callables = [s for s in self.by_file.get(path, []) if s.is_callable]
            for match in pattern.finditer(masked):
                line = _line_at(masked, match.start())
                if (path, line) in declaration_lines:
                    continue
                owner = None
                for symbol in callables:
                    if symbol.line <= line <= symbol.end_line:
                        if owner is None or symbol.line > owner.line:
                            owner = symbol
                output.append((owner, line, path))
        return output

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
                owner = _owner_at(callables, line)
                if owner is None:
                    continue
                self.edges.append((owner, name, line))

            if path.endswith(JSX_SUFFIXES):
                self._add_jsx_edges(masked, callables)

        for owner, name, _line in self.edges:
            self.calls_by_name.setdefault(name, []).append(owner)

    def _add_jsx_edges(self, masked: str, callables: list[Symbol]) -> None:
        """Treat ``<Component />`` renders as call edges.

        React components are used through JSX, not ``Component()`` calls, so
        the call regex alone sees every component as uncalled. Lowercase tags
        are host elements and are ignored.
        """
        for match in JSX_OPEN.finditer(masked):
            start = match.start(1)
            previous = masked[start - 2] if start >= 2 else ""
            # Reject type arguments such as ``Array<Foo>`` / ``useState<Foo>``;
            # a JSX tag is never directly preceded by an identifier or `)`.
            if previous and (previous.isalnum() or previous in "_$)]"):
                continue
            name = match.group(1).rsplit(".", 1)[-1]
            if len(name) < 2:
                continue
            after = match.end()
            while after < len(masked) and masked[after] in " \t":
                after += 1
            # Generic parameter lists look like ``<Props extends ...>`` or
            # ``<Props, ...>``; they are not JSX renders.
            if masked.startswith("extends", after):
                continue
            if after < len(masked) and masked[after] in "=,":
                continue
            line = _line_at(masked, start)
            owner = _owner_at(callables, line)
            if owner is None:
                continue
            self.edges.append((owner, name, line))

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

    def caller_edges(self, target: Symbol) -> list[tuple[Symbol, int]]:
        """``(caller, call_line)`` pairs for calls to *target*."""
        unique_target = self.unique_symbol(target.name)
        if unique_target is None:
            return []
        output: dict[tuple, tuple[Symbol, int]] = {}
        for owner, name, line in self.edges:
            if name != target.name or owner.key() == unique_target.key():
                continue
            output.setdefault((owner.path, owner.line, line), (owner, line))
        return sorted(output.values(), key=lambda item: (item[0].path, item[1]))

    def callee_edges(self, target: Symbol) -> list[tuple[Symbol, int]]:
        """``(callee, call_line)`` pairs for calls made by *target*."""
        output: dict[tuple, tuple[Symbol, int]] = {}
        for owner, name, line in self.edges:
            if owner.key() != target.key():
                continue
            resolved = self.unique_symbol(name)
            if resolved is None or resolved.key() == target.key():
                continue
            output.setdefault((resolved.path, resolved.line, line), (resolved, line))
        return sorted(output.values(), key=lambda item: (item[0].path, item[1]))

    def impact_edges(self, target: Symbol, depth: int = 2) -> list[tuple[Symbol, Symbol, int]]:
        """Transitive caller edges ``(caller, callee, line)`` up to *depth*."""
        output: dict[tuple, tuple[Symbol, Symbol, int]] = {}
        seen: set[tuple] = {target.key()}
        frontier = [target]
        for _ in range(max(1, depth)):
            next_frontier: list[Symbol] = []
            for current in frontier:
                for caller, line in self.caller_edges(current):
                    if caller.key() in seen:
                        continue
                    seen.add(caller.key())
                    output[(caller.path, caller.line, current.name, line)] = (
                        caller, current, line,
                    )
                    next_frontier.append(caller)
            if not next_frontier:
                break
            frontier = next_frontier
        return list(output.values())

    def call_path(self, source: Symbol, target: Symbol, max_depth: int = 6):
        """Shortest directed call path from *source* to *target*.

        Returns a list of ``(owner, callee, line)`` hops, or ``[]`` if none.
        """
        if source.key() == target.key():
            return []
        queue: list[tuple[Symbol, list[tuple[Symbol, Symbol, int]]]] = [(source, [])]
        visited: set[tuple] = {source.key()}
        while queue:
            current, path = queue.pop(0)
            if len(path) >= max_depth:
                continue
            for callee, line in self.callee_edges(current):
                hop = (current, callee, line)
                if callee.key() == target.key():
                    return path + [hop]
                if callee.key() in visited:
                    continue
                visited.add(callee.key())
                queue.append((callee, path + [hop]))
        return []

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
