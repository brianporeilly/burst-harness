"""Checks on the orchestrator's page (orchestrator/static/index.html) that need no browser."""

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "orchestrator" / "static" / "index.html"


def own_arguments(text: str, start: int) -> str:
    """The text of the call's own arguments from the "(" at start, leaving out nested calls and strings."""
    depth, quote, kept = 0, None, []
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == "\\":
                continue
            if ch == quote and text[i - 1] != "\\":
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return "".join(kept)
        elif depth == 1:
            kept.append(ch)
    raise AssertionError(f"unclosed call at {start}")


def null_children(text: str) -> list[int]:
    """Lines where replaceChildren is handed a `... : null` child, which the DOM shows as the text "null"."""
    found = []
    for match in re.finditer(r"\.replaceChildren\(", text):
        if re.search(r":\s*null\b", own_arguments(text, match.end() - 1)):
            found.append(text.count("\n", 0, match.start()) + 1)
    return found


def test_the_page_hands_replace_children_no_nulls():
    assert null_children(PAGE.read_text()) == []


def test_the_check_finds_a_null_child_and_lets_nested_ones_by():
    shown = 'box.replaceChildren(\n  el("p", {}, "a"),\n  t.sandbox ? el("p", {}, "kept") : null);'
    nested = 'box.replaceChildren(el("p", {}, t.pr ? el("a", {}, ")") : null));'
    assert null_children(shown) == [1]
    assert null_children(nested) == []
