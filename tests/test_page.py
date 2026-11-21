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


def test_a_false_boolean_attribute_is_left_out():
    """el() set every attribute, and a checkbox drawn with checked: false showed ticked (checked="false" is
    checked): a task's auto-continue and Slack boxes looked on whatever they were."""
    text = PAGE.read_text()
    helper = text[text.index("const el = (tag"):text.index("for (const child of children.flat())")]
    assert "value === false || value == null) continue" in helper and 'value === true ? "" : value' in helper


def test_what_is_hidden_stays_hidden_and_the_slack_box_is_asked_about_afresh():
    """label.inline's display: flex beat the hidden attribute, so the New task form's Slack box showed while the
    page took Slack as off (it was checked before Slack connected), and a tick in it was sent as no."""
    text = PAGE.read_text()
    assert "[hidden] { display: none !important; }" in text
    form = text[text.index("async function openTaskForm(spec)"):text.index("if (spec) fillTaskForm(spec);")]
    assert form.index("taskForm.reset()") < form.index("await loadInfo()") < form.index("taskForm.notify.checked =")
