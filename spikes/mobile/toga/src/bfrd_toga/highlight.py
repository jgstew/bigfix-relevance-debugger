"""Native syntax colouring for Toga's MultilineTextInput.

Toga has no code editor widget, but its text input *is* the platform's own
text view (UITextView on iOS, NSTextView on macOS), and both expose an
NSTextStorage. Colouring that storage gives real highlighting while keeping
native selection, keyboard, dictation and autocorrect. Best effort: any
failure only costs the colours.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

# Attribute names are the same NSString values in UIKit and AppKit.
_FG = "NSColor"
_UNDERLINE = "NSUnderline"
_UNDERLINE_COLOR = "NSUnderlineColor"

_COLORS = {
    "keyword": "systemBlueColor",
    "string": "systemRedColor",
    "number": "systemPurpleColor",
    "comment": "systemGrayColor",
    "punct": "secondaryLabelColor",
}


def _utf16_offsets(text: str) -> list[int]:
    """Code point index -> UTF-16 index (NSString counts UTF-16 units)."""
    out, n = [], 0
    for ch in text:
        out.append(n)
        n += 2 if ord(ch) > 0xFFFF else 1
    out.append(n)
    return out


def _prop(obj, name: str):
    """rubicon exposes some ObjC properties as methods (``length``, class colours)."""
    value = getattr(obj, name)
    return value() if type(value).__name__ == "ObjCBoundMethod" else value


def apply(widget, text: str, spans) -> bool:
    try:
        from rubicon.objc import ObjCClass
        from rubicon.objc.types import NSRange

        impl = widget._impl
        view = getattr(impl, "native_text", None) or impl.native  # macOS | iOS
        storage = _prop(view, "textStorage")
        to16 = _utf16_offsets(text)
        total = int(_prop(storage, "length"))
        if total != to16[-1]:
            return False  # edited since this analysis ran; the next one will catch up
        color_cls = ObjCClass("UIColor" if sys.platform == "ios" else "NSColor")
        base = _prop(color_cls, "labelColor")
        storage.beginEditing()
        try:
            everything = NSRange(0, total)
            storage.removeAttribute_range_(_UNDERLINE, everything)
            storage.addAttribute_value_range_(_FG, base, everything)
            for span in spans:
                start, end = to16[span.start], to16[min(span.end, len(text))]
                if end <= start:
                    continue
                rng = NSRange(start, end - start)
                if span.kind in ("error", "warning"):
                    under = _prop(
                        color_cls, "systemRedColor" if span.kind == "error" else "systemOrangeColor"
                    )
                    storage.addAttribute_value_range_(_UNDERLINE, 1, rng)  # single
                    storage.addAttribute_value_range_(_UNDERLINE_COLOR, under, rng)
                elif span.kind in _COLORS:
                    color = _prop(color_cls, _COLORS[span.kind])
                    storage.addAttribute_value_range_(_FG, color, rng)
        finally:
            storage.endEditing()
        return True
    except Exception:
        log.warning("highlight failed", exc_info=True)
        return False
