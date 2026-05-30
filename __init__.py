"""github-native-tools — capability-named GitHub repo ops.

Exposes ``github_repo_list / view / delete / create`` as native Hermes
tools. Auth is internal (App-installation token preferred,
GITHUB_TOKEN/GH_TOKEN fallback) so the agent never sees credentials.

This plugin replaces the in-fork ``tools/github.py`` + the 4-line
``_HERMES_CORE_TOOLS`` extension that used to live in upstream
``toolsets.py``. Same tool names, same handlers, same schemas — just
extracted to its own repo so future upstream merges of ``toolsets.py``
don't conflict on the github-tools block.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Plugin entry point — called by Hermes plugin loader at startup.

    1. Register handlers + schemas with ``tools.registry`` (side-effects
       through ``github_tools.register_tools``).
    2. Inject our 4 tool names into ``toolsets._HERMES_CORE_TOOLS`` so
       every composite toolset that references it (``hermes-core``,
       ``hermes-telegram``, ``hermes-cli`` and others — all share the
       same list-by-reference) automatically picks up github_repo_*.
    """
    # Step 1 — tool handlers
    try:
        from . import github_tools  # noqa: F401 — module is the side-effect
        github_tools.register_tools()
    except Exception as exc:
        logger.error("github-native-tools: failed to register tool handlers: %s", exc)
        return

    # Step 2 — extend _HERMES_CORE_TOOLS in-place
    try:
        import toolsets

        for name in github_tools.TOOL_NAMES:
            if name not in toolsets._HERMES_CORE_TOOLS:
                toolsets._HERMES_CORE_TOOLS.append(name)
        logger.info(
            "github-native-tools: registered %d tools and joined _HERMES_CORE_TOOLS",
            len(github_tools.TOOL_NAMES),
        )
    except Exception as exc:
        # Tools are still callable directly via toolset=github, but they
        # won't be auto-included in the composite hermes-* toolsets.
        logger.warning(
            "github-native-tools: tools registered but _HERMES_CORE_TOOLS "
            "extension failed (%s) — set enabled_toolsets: [github] manually",
            exc,
        )
