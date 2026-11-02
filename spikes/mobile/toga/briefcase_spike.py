"""Run Briefcase with its iOS Xcode-version gate relaxed. SPIKE ONLY.

Briefcase 0.4.6+ refuses iOS builds unless Xcode >= the year's App Store
minimum (Xcode 26 since April 2026). That's an App Store submission rule,
and this Mac (macOS 14.8) can't run Xcode 26, which needs macOS 15.6+. For a
simulator proof of concept, Xcode 16.2 is enough, so this lowers the floor
to 16.0. Anything that ships needs Xcode 26 regardless of the UI toolkit.

    python briefcase_spike.py create iOS
    python briefcase_spike.py run iOS -d "iPhone 16 Pro"
"""

import sys

from briefcase.__main__ import main
from briefcase.integrations.xcode import Xcode

_verify = Xcode.verify.__func__  # type: ignore[attr-defined]


def _relaxed_verify(cls, tools, min_version="13.0", **kwargs):
    return _verify(cls, tools, min_version="16.0", **kwargs)


Xcode.verify = classmethod(_relaxed_verify)  # type: ignore[method-assign]

if __name__ == "__main__":
    sys.exit(main())
