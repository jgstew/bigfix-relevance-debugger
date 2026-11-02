"""Desktop check of the shared core against the CLI's configured servers.

Runs one read-only session relevance query (default ``now``) through
``Evaluator.session`` using bigfix-remote-session-relevance's own
``load_config()`` and the CLI's pinned certificates. Unpinned certificates are
refused, never accepted. Prints connection names and results only.

    uv run -p 3.15 python scripts/real_session_check.py [relevance]
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bfrd_mobile_core.evaluate import Evaluator
from bfrd_mobile_core.secrets import MemorySecretStore
from bigfix_remote_session_relevance import load_config
from bigfix_remote_session_relevance.trust import default_known_certs_path

log = logging.getLogger("real_session_check")


async def main(text: str) -> int:
    configs = list(load_config().enabled_connections())
    log.info("%d configured connection(s): %s", len(configs), ", ".join(c.name for c in configs))
    ev = Evaluator(MemorySecretStore(), known_certs_path=str(default_known_certs_path()))
    ok = 0
    async for row in ev.session(text, [], configs=configs, timeout=30):
        ok += row.ok
        result = ", ".join(row.answers[:3]) if row.ok else f"{row.error_kind}: {row.error}"
        log.info("  %-24s %s %s", row.connection, "OK " if row.ok else "ERR", result)
    if ev.pending_certs:
        log.warning("refused unpinned certs: %s", ", ".join(ev.pending_certs))
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "now")))
