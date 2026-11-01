"""Load the agent prompt templates packaged with DevPlane."""

from __future__ import annotations

from importlib import resources


def load_template(name: str) -> str:
    """Return the text of a template shipped in ``devplane/templates``."""
    return (resources.files(__package__) / "templates" / name).read_text(encoding="utf-8")


def render_template(name: str, **values: str) -> str:
    """Interpolate ``values`` into a template.

    Only the trusted template is parsed for placeholders, so braces inside task
    text or other interpolated values stay literal. A missing value raises
    ``KeyError`` rather than sending an agent an incomplete contract.
    """
    return load_template(name).format(**values)
