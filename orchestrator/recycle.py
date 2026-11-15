"""Keep a cluster's warm pools on their templates: every `interval` seconds, the idle sandboxes of the pools
tasks and sessions claim from (pool, root_pool) that are out of date with their template are replaced
(harness/warm_pool.py). After an image bump, the next claim gets the new image without anyone recycling
the pool by hand. ORCHESTRATOR_RECYCLE sets the interval (default 300; 0 turns it off).
"""

from __future__ import annotations

import sys
import threading
import time

from harness import warm_pool
from harness.agent_sandbox import AgentSandboxError
from harness.config import HarnessConfig

ENV = "ORCHESTRATOR_RECYCLE"
INTERVAL = 300


class Recycler:
    def __init__(self, base: HarnessConfig, interval: int = INTERVAL) -> None:
        self.base, self.interval = base, interval
        self.pools = tuple(dict.fromkeys(p for p in (base.pool, base.root_pool) if p))
        self.last_error = ""

    def once(self) -> list[str]:
        """Look once; what was done, a line each. A failure is said once, not every time it happens again."""
        try:
            done = warm_pool.recycle(self.base, self.pools)
        except (AgentSandboxError, ValueError) as err:
            message = f"could not look at the warm pools {', '.join(self.pools)}: {err}"
            if message == self.last_error:
                return []
            self.last_error = message
            return [message]
        self.last_error = ""
        return done

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    for line in self.once():
                        print(f"warm pools: {line}", file=sys.stderr, flush=True)
                except Exception as err:  # noqa: BLE001 -- the next look tries again
                    print(f"warm pools: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
                time.sleep(self.interval)

        threading.Thread(target=loop, name="warm-pools", daemon=True).start()
