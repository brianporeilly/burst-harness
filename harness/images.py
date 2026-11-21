"""Looking at an image for the Strands engine: a view_image tool, given to a run whose model takes images, that reads
a screenshot (web-shot's, window-shot's, a game's own) from where the agent works and puts it in front of the model.
Claude Code reads image files itself (its Read)."""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from typing import TYPE_CHECKING, Any

from strands import tool

if TYPE_CHECKING:
    from .config import HarnessConfig
    from .exec_target import ExecTarget

MAX_BYTES = 4 * 2**20  # what one image may weigh: the providers' own limits are about 5 MB, before base64
FORMATS = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))

# What a llama-server said of its images, by its URL: (when asked, whether it takes them).
_ASKED: dict[str, tuple[float, bool]] = {}
ASKED_FOR = 60  # seconds before it is asked again: it may have been restarted with another model


def sees_images(cfg: HarnessConfig) -> bool:
    """Whether the run's model takes images. `vision` "yes" or "no" says so; on "auto", Anthropic's models do, a
    llama-server says (its /props: modalities.vision, true with a projector loaded), and other providers are taken
    not to."""
    setting = (cfg.vision or "auto").strip().lower()
    if setting in ("yes", "no"):
        return setting == "yes"
    if cfg.provider == "anthropic":
        return True
    if cfg.provider != "llamacpp":
        return False
    from .models import llamacpp_url

    root = llamacpp_url(cfg).rstrip("/").removesuffix("/v1")
    known = _ASKED.get(root)
    if known is not None and time.monotonic() - known[0] <= ASKED_FOR:
        return known[1]
    try:
        with urllib.request.urlopen(root + "/props", timeout=5) as response:
            props = json.loads(response.read(2**20))
        modalities = props.get("modalities") if isinstance(props, dict) else None
        sees = isinstance(modalities, dict) and modalities.get("vision") is True
    except (OSError, ValueError):
        sees = False
    _ASKED[root] = (time.monotonic(), sees)
    return sees


def image_format(data: bytes) -> str | None:
    """png, jpeg, gif or webp, from the file's first bytes; None for anything else."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((name for magic, name in FORMATS if data.startswith(magic)), None)


def read_image(target: ExecTarget, path: str) -> dict[str, Any]:
    """A Strands tool result: the image at `path` (in the repo, or absolute) with a line about it, or an error
    saying why not."""
    def error(text: str) -> dict[str, Any]:
        return {"status": "error", "content": [{"text": text}]}

    import shlex

    # The size first, so a large file is refused without being sent through the exec.
    quoted = shlex.quote(path)
    found = target.run_shell(f"test -f {quoted} && wc -c < {quoted}", check=False)
    if found.returncode != 0 or not found.stdout.strip().isdigit():
        return error(f"No file at {path}.")
    size = int(found.stdout.strip())
    if size > MAX_BYTES:
        return error(f"{path} is {size / 2**20:.1f} MiB, over the {MAX_BYTES // 2**20} MiB an image may be: take a "
                     "smaller one (a smaller --size, or a JPEG).")
    read = target.run_shell(f"base64 < {quoted} | tr -d '\\n'", check=False)
    try:
        data = base64.b64decode(read.stdout.strip(), validate=True) if read.returncode == 0 else b""
    except ValueError:
        data = b""
    if not data:
        return error(f"Could not read {path}.")
    kind = image_format(data)
    if kind is None:
        return error(f"{path} is not a PNG, JPEG, GIF or WebP image.")
    return {"status": "success", "content": [
        {"text": f"{path} ({kind.upper()}, {len(data) / 1024:.0f} KiB):"},
        {"image": {"format": kind, "source": {"bytes": data}}},
    ]}


def view_image_tool(target: ExecTarget) -> Any:
    @tool
    def view_image(path: str) -> dict[str, Any]:
        """Look at an image file: a screenshot from web-shot or window-shot, or one the program saved. Each image
        stays in the conversation and costs about as much as a page of text, so look at the ones that answer a
        question, not every one.

        Args:
            path: The image's path, in the repo or absolute: a PNG, JPEG, GIF or WebP file of at most 4 MiB.
        """
        return read_image(target, path)

    return view_image
