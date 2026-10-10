"""A browser GUI for BigFix client relevance.

Live static analysis via ``bigfix-relevance-analyzer``, evaluation with real
``qna`` via ``bigfix-remote-client-relevance``, and a per-level breakdown
that shows where an expression goes empty on a given endpoint.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("bigfix-relevance-debugger")
except PackageNotFoundError:  # pragma: no cover - only when run from a source tree
    __version__ = "0.0.0+unknown"
