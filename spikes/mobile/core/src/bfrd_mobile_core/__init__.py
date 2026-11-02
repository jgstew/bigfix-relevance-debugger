"""UI-free core for the mobile/desktop proofs of concept (issue #33).

Every UI (Toga, Kivy, the Swift shell over plain CPython) calls only this
package, so the comparison is about the UI toolkits, not the BigFix code:

- :mod:`.analysis`: live static analysis and highlight spans (analyzer).
- :mod:`.connections`: saved connections (no secrets) in the app data dir.
- :mod:`.secrets`: passwords in the Keychain (ctypes, macOS and iOS alike).
- :mod:`.evaluate`: session relevance and Fast Query client relevance.
- :mod:`.bridge`: a JSON-in/JSON-out facade for hosts that aren't Python.
"""

from __future__ import annotations

import sys

IS_IOS = sys.platform == "ios"
IS_ANDROID = sys.platform == "android"
IS_MOBILE = IS_IOS or IS_ANDROID

PACKAGES = (
    "bigfix-relevance-analyzer",
    "bigfix-remote-session-relevance",
    "bigfix-remote-client-relevance",
)


def versions() -> dict[str, str]:
    """Python, platform and the BigFix libraries' installed versions."""
    from importlib.metadata import PackageNotFoundError, version

    out = {"python": sys.version.split()[0], "platform": sys.platform}
    for name in PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = "not installed"
    return out
