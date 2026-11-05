"""Text utilities: JSON extraction, issue cleaning, path normalisation, keywords."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "when", "where", "what",
    "which", "while", "have", "has", "had", "not", "but", "are", "was", "were",
    "will", "would", "should", "could", "can", "may", "might", "must", "does",
    "did", "doing", "been", "being", "into", "onto", "than", "then", "there",
    "their", "them", "they", "these", "those", "your", "you", "our", "its",
    "it", "is", "in", "on", "of", "to", "a", "an", "as", "at", "by", "or",
    "be", "if", "we", "us", "so", "no", "all", "any", "some", "also", "after",
    "before", "because", "about", "over", "under", "up", "down", "out", "off",
    "error", "errors", "issue", "issues", "problem", "bug", "fix", "fixed",
    "fixes", "code", "file", "files", "class", "method", "function", "test",
    "tests", "add", "added", "adds", "update", "updated", "updates", "change",
    "changed", "changes", "new", "old", "use", "used", "using", "make", "made",
    "support", "version", "release", "could", "would", "please", "expected",
    "actual", "steps", "reproduce", "description", "summary", "background",
    "type", "documentation", "checklist", "motivation", "context", "additional",
    "screenshots", "testing", "na", "todo", "current", "behavior", "behaviour",
}

REF_RE = re.compile(
    r"(?:(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+))?#(?P<num>\d+)"
)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
FILE_RE = re.compile(r"^\+\+\+ b/(.+?)(?:\t.*)?$")


def clean_text(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_refs(text: str | None) -> list[tuple[str | None, str | None, int]]:
    refs: list[tuple[str | None, str | None, int]] = []
    for match in REF_RE.finditer(text or ""):
        owner, name = match.group("owner"), match.group("name")
        if owner and owner.lower() in {"http", "https", "www"}:
            continue
        refs.append((owner, name, int(match.group("num"))))
    return refs


def parse_numstat(text: str) -> list[tuple[str, int]]:
    files: list[tuple[str, int]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        try:
            total = (int(added) if added != "-" else 0) + (
                int(deleted) if deleted != "-" else 0
            )
        except ValueError:
            total = 0
        files.append((path, total))
    return files


def parse_hunks(diff: str) -> dict[str, list[list[int]]]:
    hunks: dict[str, list[list[int]]] = {}
    current = None
    for line in diff.splitlines():
        file_match = FILE_RE.match(line)
        if file_match:
            current = file_match.group(1).strip()
            hunks.setdefault(current, [])
            continue
        if line.startswith("@@") and current:
            match = HUNK_RE.match(line)
            if match:
                start = int(match.group(1))
                length = int(match.group(2) or 1)
                if length <= 0:
                    length = 1
                hunks[current].append([start, start + length - 1])
    return {key: value for key, value in hunks.items() if value}


def extract_json(text: str):
    """Return the first JSON value embedded in *text*, or ``None``."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def render(value, ctx: dict) -> str:
    """Substitute ``{key}`` placeholders in a config string."""
    rendered = str(value)
    for key, replacement in ctx.items():
        rendered = rendered.replace("{" + key + "}", str(replacement))
    return rendered


def split_terms(text: str) -> tuple[list[str], list[str]]:
    """Split text into high-signal identifiers and ordinary words."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    symbols: list[str] = []
    words: list[str] = []
    seen: set[str] = set()

    def add(bucket: list[str], term: str) -> None:
        key = term.lower()
        if key in seen or key in STOPWORDS:
            return
        seen.add(key)
        bucket.append(term)

    for match in re.finditer(r"`([^`\n]{2,80})`", text):
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", match.group(1)):
            if "_" in token or re.search(r"[a-z][A-Z]", token) or token.isupper():
                add(symbols, token)
            else:
                add(words, token)

    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        token = match.group(0)
        if token.lower() in STOPWORDS:
            continue
        if "_" in token or re.search(r"[a-z][A-Z]", token):
            add(symbols, token)
        elif token.isupper() and len(token) > 3:
            add(symbols, token)
        else:
            add(words, token)
    return symbols, words


def keywords(text: str, limit: int = 12) -> list[str]:
    symbols, words = split_terms(text)
    output: list[str] = []
    lowered: set[str] = set()
    for word in symbols + words:
        key = word.lower()
        if key in lowered:
            continue
        lowered.add(key)
        output.append(word)
        if len(output) >= limit:
            break
    return output


def trim_problem(text: str) -> str:
    """Drop PR-template tail sections that dilute a natural-language query."""
    text = re.split(
        r"\n#{2,4}\s*(?:Checklist|Type of change|Screenshots)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    return text.strip()


def normalize_path(path, repo) -> str:
    """Normalise a tool-returned path to a POSIX path relative to *repo*."""
    if path is None:
        return ""
    value = str(path).strip().strip("\"'")
    if not value:
        return ""
    if value.startswith("file://"):
        value = value[7:]
    value = value.replace("\\", "/")
    if os.path.isabs(value):
        try:
            value = os.path.relpath(value, str(repo))
        except ValueError:
            return value.replace("\\", "/")
    value = re.sub(r"^\./", "", value)
    return value.replace("\\", "/")


def path_in_scope(path: str, repo_cfg: dict | None) -> bool:
    """Apply repository include/exclude globs to a tool-returned path."""
    import fnmatch

    repo_cfg = repo_cfg or {}
    value = str(path or "").replace("\\", "/")
    includes = repo_cfg.get("include") or []
    excludes = repo_cfg.get("exclude") or []

    def matches(pattern: str) -> bool:
        pattern = str(pattern).replace("\\", "/")
        return (
            fnmatch.fnmatch(value, pattern)
            or fnmatch.fnmatch("/" + value, pattern)
        )

    if includes and not any(matches(pattern) for pattern in includes):
        return False
    return not any(matches(pattern) for pattern in excludes)


def is_test_path(path: str, patterns: list[str] | None = None) -> bool:
    value = "/" + str(path).replace("\\", "/").lstrip("/")
    default = ["/src/test/", "/src/it/", "/test/", "/tests/", "__tests__"]
    patterns = default + list(patterns or [])
    for marker in patterns:
        marker = marker.replace("\\", "/")
        if marker.startswith("**/"):
            marker = marker[3:]
        if marker.endswith("/**"):
            marker = marker[:-3]
        marker = marker.strip("/")
        if not marker:
            continue
        if "/" in marker or "*" not in marker:
            if f"/{marker}/" in value or value.rstrip("/").endswith(f"/{marker}"):
                return True
        elif marker in value:
            return True
    name = Path(value).name
    return name.endswith(("Test.java", "Tests.java", "IT.java", "_test.py", ".test.js", ".test.ts"))
