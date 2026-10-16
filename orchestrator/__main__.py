"""python -m orchestrator [--port 8700]: the orchestrator, on this machine only.

Credentials it can hand to sessions come from its own environment, as for the
harness: HARNESS_GIT_TOKEN (or GH_TOKEN), ANTHROPIC_API_KEY,
CLAUDE_CODE_OAUTH_TOKEN. Your ~/.config/burst-harness/config.toml supplies the
defaults (model, images, egress_allow).
"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .backend import PodmanBackend


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8700)
    args = parser.parse_args()
    print(f"Orchestrator on http://127.0.0.1:{args.port}/ (this machine only)")
    # Loopback only: there is no login (see app.py).
    uvicorn.run(create_app(PodmanBackend()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
