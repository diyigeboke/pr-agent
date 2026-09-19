"""Targeted reference context: show a reviewer who else uses the symbols it is changing.

The review prompt only ever sees diff hunks, so the model cannot tell whether a caller
elsewhere already guarantees a precondition (the usual source of "this can be null"
false positives). This module closes part of that gap without an index: it pulls the
symbol names introduced or modified by the diff, greps a local checkout for other
references to them, and renders a bounded block that is injected into the prompt.

It is deliberately a grep, not a symbol graph: it matches by name, so it cannot resolve
overloads, and for very common names the hits are capped rather than exhaustive. Bounds
(symbol count, hits per symbol, total characters, scanned files) keep the cost predictable.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Directories that never carry first-party source worth grepping.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "target", "build", "dist", "out",
    "__pycache__", ".venv", "venv", ".idea", ".gradle", "vendor",
}

_DEFAULT_EXTENSIONS = (
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".py", ".go", ".rs", ".rb", ".php", ".cs",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".c", ".cc", ".cpp", ".h", ".hpp",
)

# Type declarations: class/interface/enum/record Foo
_TYPE_RE = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
# Method declarations: a visibility/modifier list, a return type, then name(
_METHOD_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|async)\s+)+"
    r"[\w<>\[\],.?&\s]*?([a-z_$][\w$]*)\s*\("
)
# Names too common to be worth grepping.
_STOPWORDS = {
    "if", "for", "while", "switch", "catch", "return", "new", "this", "super",
    "get", "set", "of", "to", "from", "builder", "equals", "hashCode", "toString",
    "main", "run", "call", "apply", "test", "init", "create", "update", "delete",
}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_changed_symbols(patch: str, filename: str = "") -> List[str]:
    """Return type and method names declared in the patch's added lines, most specific first.

    Only added lines are inspected: a name appearing solely in the removed side is not
    something the PR introduces. Class names derived from the file name are included so a
    change to a type's body also surfaces its callers.
    """
    symbols: List[str] = []
    seen = set()

    def _add(name: str) -> None:
        if name and name not in seen and name.lower() not in _STOPWORDS and len(name) > 2:
            seen.add(name)
            symbols.append(name)

    for line in (patch or "").splitlines():
        if not line.startswith("+"):
            continue
        body = line[1:]
        for match in _TYPE_RE.finditer(body):
            _add(match.group(1))
        match = _METHOD_RE.match(body)
        if match:
            _add(match.group(1))

    if filename:
        stem = Path(filename).stem
        if stem and stem not in ("index",):
            _add(stem)

    return symbols


def _scannable(path: Path, extensions: Sequence[str]) -> bool:
    return path.suffix.lower() in extensions


def find_references(
    root: Path,
    symbol: str,
    max_hits: int,
    extensions: Sequence[str],
    skip_file: str = "",
    max_scanned_files: int = 20000,
    context_lines: int = 0,
) -> List[Tuple[str, int, int, str]]:
    """Grep ``root`` for other references to ``symbol``.

    Returns (relative_path, first_line, last_line, block_text) tuples, where block_text
    holds the hit plus ``context_lines`` of surrounding source on each side. Callers that
    want to judge *why* a call site is safe (an up-front null check, an already-batched
    input) need that surrounding code: the single matching line rarely carries it.

    Nearby hits in the same file are merged into one block so shared context is not repeated.
    Matching is by word boundary, so ``Foo`` does not match ``FooBar``.
    """
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    context_lines = max(0, context_lines)
    results: List[Tuple[str, int, int, str]] = []
    scanned = 0
    sites = 0
    skip_abs = os.path.abspath(skip_file) if skip_file else ""

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if not _scannable(path, extensions):
                continue
            if skip_abs and os.path.abspath(path) == skip_abs:
                continue
            scanned += 1
            if scanned > max_scanned_files:
                get_logger().warning(
                    f"reference context: scanned {max_scanned_files} files, stopping lookup for {symbol}"
                )
                return results
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            lines = text.splitlines()
            matched = [idx for idx, line in enumerate(lines) if pattern.search(line)]
            if not matched:
                continue

            # max_hits bounds reference *sites*, not rendered blocks: merging below is a
            # rendering optimisation, so counting blocks would let one dense region blow
            # past the intended output bound.
            remaining = max_hits - sites
            if remaining <= 0:
                return results
            matched = matched[:remaining]
            sites += len(matched)

            # Merge hits that would share context, so one region is emitted once.
            groups: List[List[int]] = []
            for idx in matched:
                if groups and idx - groups[-1][-1] <= 2 * context_lines + 1:
                    groups[-1].append(idx)
                else:
                    groups.append([idx])

            # Emit POSIX separators: these paths sit next to diff file names, which
            # always use forward slashes regardless of the host platform.
            rel = path.relative_to(root).as_posix()
            for group in groups:
                first = max(0, group[0] - context_lines)
                last = min(len(lines) - 1, group[-1] + context_lines)
                block = "\n".join(f"        {line}" for line in lines[first:last + 1])
                results.append((rel, first + 1, last + 1, block))
    return results


def build_reference_context(diff_files: Iterable, max_chars: int | None = None) -> str:
    """Render the reference-context block for the given diff files, or "" when disabled.

    ``diff_files`` are FilePatchInfo-like objects exposing ``filename`` and ``patch``.
    The whole block is skipped when ``config.reference_context_root`` is unset so the
    default behaviour is unchanged.
    """
    root_value = get_settings().config.get("reference_context_root", "") or ""
    if not root_value:
        return ""
    root = Path(root_value)
    if not root.is_dir():
        get_logger().warning(f"reference context: {root_value} is not a directory; skipping")
        return ""

    max_symbols = _as_int(get_settings().config.get("reference_context_max_symbols", 10), 10)
    max_hits = _as_int(get_settings().config.get("reference_context_max_hits_per_symbol", 5), 5)
    context_lines = _as_int(get_settings().config.get("reference_context_context_lines", 3), 3)
    if max_chars is None:
        max_chars = _as_int(get_settings().config.get("reference_context_max_chars", 6000), 6000)
    extensions = get_settings().config.get("reference_context_extensions", None) or _DEFAULT_EXTENSIONS
    extensions = tuple(str(ext).lower() for ext in extensions)

    sections: List[str] = []
    used = 0
    for diff_file in diff_files:
        filename = getattr(diff_file, "filename", "") or ""
        symbols = extract_changed_symbols(getattr(diff_file, "patch", "") or "", filename)[:max_symbols]
        for symbol in symbols:
            hits = find_references(
                root, symbol, max_hits, extensions,
                skip_file=filename, context_lines=context_lines,
            )
            if not hits:
                continue
            rendered = "\n".join(
                f"    {path}:{first}-{last}\n{source}" for path, first, last, source in hits
            )
            section = f"- `{symbol}` is also referenced in:\n{rendered}"
            if used + len(section) > max_chars:
                get_logger().info("reference context: character budget reached; truncating")
                return "\n".join(sections)
            sections.append(section)
            used += len(section)

    if not sections:
        return ""
    get_logger().info(f"reference context: {len(sections)} symbol(s) with external references")
    return "\n".join(sections)
