"""Configurable energy-bill generation (config → engine → renderers).

Pure Python — no Home Assistant imports anywhere in this package, so the
whole billing pipeline is unit-testable standalone. The ``generate_bill``
service in ``services.py`` is the only HA-facing entry point.
"""

from __future__ import annotations

from collections.abc import Callable

from .config import FORMAT_CSV, FORMAT_MARKDOWN, Profile
from .engine import BillResult
from .render_csv import render_csv
from .render_markdown import render_markdown

#: Formats that can actually be rendered today. PDF (accepted by the config
#: schema for forward compatibility) joins in a later release.
RENDERERS: dict[str, Callable[[BillResult, Profile], str]] = {
    FORMAT_MARKDOWN: render_markdown,
    FORMAT_CSV: render_csv,
}

FILE_EXTENSIONS = {FORMAT_MARKDOWN: "md", FORMAT_CSV: "csv"}


def render_bill(result: BillResult, profile: Profile, fmt: str) -> str:
    """Render a computed bill in the given format."""
    try:
        renderer = RENDERERS[fmt]
    except KeyError:
        raise ValueError(f"no renderer for format {fmt!r}") from None
    return renderer(result, profile)
