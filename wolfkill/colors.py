from __future__ import annotations

import sys

_FORCE_COLOR: bool | None = None


def _color_enabled() -> bool:
    if _FORCE_COLOR is not None:
        return _FORCE_COLOR
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _wrap(code: str, text: str) -> str:
    if not _color_enabled():
        return text
    return f"[{code}m{text}[0m"


def bold(text: str) -> str:
    return _wrap("1", text)


def dim(text: str) -> str:
    return _wrap("2", text)


def yellow(text: str) -> str:
    return _wrap("33", text)


def cyan(text: str) -> str:
    return _wrap("36", text)


def green(text: str) -> str:
    return _wrap("32", text)


def red(text: str) -> str:
    return _wrap("31", text)


def magenta(text: str) -> str:
    return _wrap("35", text)


def blue(text: str) -> str:
    return _wrap("34", text)


def bright_green(text: str) -> str:
    return _wrap("92", text)


def bright_cyan(text: str) -> str:
    return _wrap("96", text)


def bright_yellow(text: str) -> str:
    return _wrap("93", text)


def bright_magenta(text: str) -> str:
    return _wrap("95", text)


def bright_blue(text: str) -> str:
    return _wrap("94", text)


def bright_red(text: str) -> str:
    return _wrap("91", text)


_SEAT_COLORS = (
    "32",
    "36",
    "33",
    "95",
    "94",
    "91",
    "92",
    "96",
    "93",
)


def seat_color(seat: str, text: str) -> str:
    try:
        idx = int(str(seat).lstrip("pP")) - 1
    except (ValueError, TypeError, AttributeError):
        return text
    return _wrap(_SEAT_COLORS[idx % len(_SEAT_COLORS)], text)
