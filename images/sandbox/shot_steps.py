"""The steps web-shot and window-shot take after their wait: input to send, pauses and screenshots, in order.

  tap:X,Y        a click (or a tap, with web-shot --touch) at X,Y
  key:NAME       a key press: Space, Enter, ArrowLeft, a, ... (window-shot: X key names, also Left, Return)
  type:TEXT      text typed a key at a time
  wait:SECONDS   a pause
  shot:FILE      a screenshot, besides the one taken last into --out
"""

import re

STEP = re.compile(r"(tap):(\d+),(\d+)|(key|type):(.+)|(wait):(\d+(?:\.\d+)?)|(shot):(.+)", re.DOTALL)


def parse(text: str) -> tuple[str, ...]:
    """A step as (kind, *values), or ValueError saying what steps there are."""
    match = STEP.fullmatch(text)
    if not match:
        raise ValueError(f"{text!r} is not a step: tap:X,Y, key:NAME, type:TEXT, wait:SECONDS or shot:FILE.")
    groups = [g for g in match.groups() if g is not None]
    return tuple(groups)
