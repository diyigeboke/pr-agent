"""Forward (after) dynamic context: extend a hunk down to the end of the block it changes.

The before-side already extends up to the enclosing declaration. Without the matching
extension below, the reviewer sees where a block starts but never where it ends — so it
cannot tell whether the code after the change still assumes the old behaviour.
"""
from pr_agent.algo.git_patch_processing import (
    _dynamic_after_extra_lines,
    _resolve_after_extra,
    _strip_code_literals,
    process_patch_lines,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = ["config.max_extra_lines_after_dynamic_context", "config.allow_dynamic_context"]

ORIGINAL = """public class Small {
    public void shortMethod() {
        int a = 1;
        int b = 2;
        int c = 3;
        if (a > 0) {
            return;
        }
        System.out.println(a + c);
    }

    public void other() {
        // unrelated
    }
}
"""

PATCH = """diff --git a/Small.java b/Small.java
--- a/Small.java
+++ b/Small.java
@@ -1,7 +1,7 @@
 public class Small {
     public void shortMethod() {
         int a = 1;
-        int b = 2;
+        int b = 22;
         int c = 3;
         if (a > 0) {
             return;
"""


def _context_after_change(result) -> list:
    lines = result if isinstance(result, list) else result.split("\n")
    text = "\n".join(lines)
    idx = text.find("int b = 22;")
    return [l for l in text[idx:].split("\n")[1:] if l.startswith(" ")]


# --- literal stripping -------------------------------------------------------

def test_literals_and_comments_do_not_count_as_braces():
    assert _strip_code_literals('foo("}");') == "foo();"
    assert _strip_code_literals("bar('}');") == "bar();"
    assert _strip_code_literals("int x = 1; // trailing }") == "int x = 1; "
    assert _strip_code_literals("if (a) {") == "if (a) {"


# --- block-end lookup --------------------------------------------------------

def test_finds_the_end_of_the_block_containing_the_anchor():
    lines = ORIGINAL.splitlines()
    # anchor on the changed line ("int b = 2;" is index 3), hunk ends at index 6
    extra = _dynamic_after_extra_lines(lines, 3, 6, cap=20)
    # the enclosing method closes at index 9 ("    }"), so 9 - 6 + 1 = 4 more lines
    assert extra == 4


def test_cap_bounds_the_extension():
    lines = ORIGINAL.splitlines()
    # the block closes 4 lines down; a smaller cap truncates rather than giving up
    assert _dynamic_after_extra_lines(lines, 3, 6, cap=2) == 2
    assert _dynamic_after_extra_lines(lines, 3, 6, cap=4) == 4


def test_returns_zero_when_no_enclosing_block_is_found():
    assert _dynamic_after_extra_lines(["no braces here"], 0, 0, cap=10) == 0
    assert _dynamic_after_extra_lines([], 0, 0, cap=10) == 0
    assert _dynamic_after_extra_lines(["a{"], 0, 0, cap=0) == 0


# --- resolution against the fixed setting ------------------------------------

def test_disabled_falls_back_to_the_fixed_setting():
    lines = ORIGINAL.splitlines()
    assert _resolve_after_extra(lines, 1, 7, 3, fixed=1, allow_dynamic=False, cap=10) == 1
    assert _resolve_after_extra(lines, 1, 7, 3, fixed=1, allow_dynamic=True, cap=0) == 1


def test_missing_anchor_falls_back_to_the_fixed_setting():
    lines = ORIGINAL.splitlines()
    assert _resolve_after_extra(lines, 1, 7, -1, fixed=2, allow_dynamic=True, cap=10) == 2


# --- end to end through process_patch_lines ----------------------------------

def test_patch_extends_to_the_block_end_when_enabled():
    snapshot = snapshot_settings(KEYS)
    try:
        get_settings().set("config.allow_dynamic_context", True)
        get_settings().set("config.max_extra_lines_after_dynamic_context", 10)
        after = _context_after_change(process_patch_lines(PATCH, ORIGINAL, 0, 1, ORIGINAL))
        assert after[-1].strip() == "}", "the enclosing method must be closed"
        assert "System.out.println" in "\n".join(after)
    finally:
        restore_settings(snapshot)


def test_patch_keeps_the_fixed_setting_when_disabled():
    snapshot = snapshot_settings(KEYS)
    try:
        get_settings().set("config.allow_dynamic_context", True)
        get_settings().set("config.max_extra_lines_after_dynamic_context", 0)
        after = _context_after_change(process_patch_lines(PATCH, ORIGINAL, 0, 1, ORIGINAL))
        # git's own 3-line context already closes the inner `if`, so assert on the
        # method body instead: without the extension the tail of the method is cut off
        assert "System.out.println" not in "\n".join(after)
        assert len(after) < 6
    finally:
        restore_settings(snapshot)
