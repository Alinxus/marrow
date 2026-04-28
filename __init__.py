"""
Package bootstrap for Marrow.

The codebase historically used repo-local imports like `import config` and
`from storage import db`. Those work when running from the repository root,
but they break when Marrow is launched as an installed package on macOS/Linux.

We alias the package-internal modules into `sys.modules` so existing imports
continue to resolve without requiring a risky whole-repo import rewrite.
"""

from __future__ import annotations

import importlib
import sys


def _alias(name: str, relative_target: str) -> None:
    if name in sys.modules:
        return
    sys.modules[name] = importlib.import_module(relative_target, __name__)


for _name, _target in (
    ("config", ".config"),
    ("on_demand", ".on_demand"),
    ("actions", ".actions"),
    ("brain", ".brain"),
    ("capture", ".capture"),
    ("personality", ".personality"),
    ("storage", ".storage"),
    ("ui", ".ui"),
    ("voice", ".voice"),
):
    try:
        _alias(_name, _target)
    except Exception:
        # Keep package import resilient even if an optional area is broken.
        pass
