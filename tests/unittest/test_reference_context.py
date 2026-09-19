"""Targeted reference context: bounded cross-file lookups for symbols changed by the PR."""
from types import SimpleNamespace

from pr_agent.algo.reference_context import (
    build_reference_context,
    extract_changed_symbols,
    find_references,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = [
    "config.reference_context_root",
    "config.reference_context_max_symbols",
    "config.reference_context_max_hits_per_symbol",
    "config.reference_context_max_chars",
    "config.reference_context_extensions",
]


def _diff_file(filename, patch):
    return SimpleNamespace(filename=filename, patch=patch)


# --- symbol extraction -------------------------------------------------------

def test_extracts_type_and_method_names_from_added_lines():
    patch = (
        "@@ -1,3 +1,5 @@\n"
        "-    oldCall();\n"
        "+public class NoticeTmplAggregator {\n"
        "+    public static int countByEventIds(List<String> ids) {\n"
        "+        return ids.size();\n"
    )
    symbols = extract_changed_symbols(patch, "NoticeTmplAggregator.java")
    assert "NoticeTmplAggregator" in symbols
    assert "countByEventIds" in symbols


def test_ignores_removed_lines():
    """A name only present on the '-' side is not something the PR introduces."""
    patch = "@@ -1,2 +1,1 @@\n-public void removedHelper() {\n+int x = 1;\n"
    symbols = extract_changed_symbols(patch, "")
    assert "removedHelper" not in symbols


def test_drops_stopwords_and_short_names():
    patch = "+if (get(x)) { return; }\n+int a = 1;\n"
    symbols = extract_changed_symbols(patch, "")
    for noise in ("if", "get", "return", "a"):
        assert noise not in symbols


# --- reference lookup --------------------------------------------------------

def test_find_references_matches_word_boundaries_and_skips_the_changed_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Caller.java").write_text(
        "class Caller { void go() { NoticeTmplAggregator.countByEventIds(ids); } }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "Unrelated.java").write_text(
        "class Unrelated { int countByEventIdsExtra; }\n", encoding="utf-8"
    )
    (tmp_path / "src" / "Self.java").write_text(
        "class Self { void f() { countByEventIds(); } }\n", encoding="utf-8"
    )

    hits = find_references(
        tmp_path, "countByEventIds", max_hits=10,
        extensions=(".java",), skip_file=str(tmp_path / "src" / "Self.java"),
    )
    paths = [p for p, _, _ in hits]
    assert "src/Caller.java" in paths
    assert "src/Self.java" not in paths, "the file under review must be excluded"
    assert "src/Unrelated.java" not in paths, "word boundaries must prevent suffix matches"


def test_find_references_respects_the_cap(tmp_path):
    (tmp_path / "Hit.java").write_text(
        "\n".join("void f%d() { Foo.bar(); }" % i for i in range(20)), encoding="utf-8"
    )
    hits = find_references(tmp_path, "Foo", max_hits=3, extensions=(".java",))
    assert len(hits) == 3


# --- rendering ---------------------------------------------------------------

def test_build_is_disabled_without_a_configured_root(tmp_path):
    snapshot = snapshot_settings(KEYS)
    try:
        get_settings().set("config.reference_context_root", "")
        result = build_reference_context([_diff_file("A.java", "+class A {}")])
        assert result == ""
    finally:
        restore_settings(snapshot)


def test_build_renders_references_for_a_configured_root(tmp_path):
    snapshot = snapshot_settings(KEYS)
    try:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "Caller.java").write_text(
            "class Caller { void go() { Helper.run(); } }\n", encoding="utf-8"
        )
        get_settings().set("config.reference_context_root", str(root))
        get_settings().set("config.reference_context_extensions", [".java"])

        result = build_reference_context([_diff_file("Helper.java", "+class Helper {}")])
        assert "Helper" in result
        assert "Caller.java" in result
    finally:
        restore_settings(snapshot)


def test_build_returns_empty_when_nothing_references_the_change(tmp_path):
    snapshot = snapshot_settings(KEYS)
    try:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "Elsewhere.java").write_text("class Elsewhere {}\n", encoding="utf-8")
        get_settings().set("config.reference_context_root", str(root))
        get_settings().set("config.reference_context_extensions", [".java"])

        result = build_reference_context([_diff_file("Lonely.java", "+class Lonely {}")])
        assert "Lonely" not in result
    finally:
        restore_settings(snapshot)
