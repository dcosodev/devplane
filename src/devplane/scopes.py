"""Bounded write-scope globs: exact membership and overlap decisions.

A scope is a POSIX-style, project-relative pattern in a deliberately small
grammar so that every question below has an exact answer:

* literal path segments;
* ``*`` (any run of characters) and ``?`` (exactly one character) inside a
  segment, never crossing ``/``;
* ``**`` only as the *final* segment, matching one or more further segments.

Anything outside that grammar (character classes, braces, negation, ``**`` in
the middle, absolute or escaping paths) is rejected by :func:`validate_scope`
instead of being interpreted loosely.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache

from .core import DevPlaneError

MAX_SCOPE_LENGTH = 1024
_FORBIDDEN_CHARACTERS = frozenset("[]{}!\\")
_TREE = "**"


def _split_scope(scope: str) -> list[str]:
    if not isinstance(scope, str) or not scope:
        raise DevPlaneError("scope must be a non-empty string")
    if len(scope) > MAX_SCOPE_LENGTH:
        raise DevPlaneError(f"scope exceeds {MAX_SCOPE_LENGTH} characters")
    if scope.startswith("/"):
        raise DevPlaneError(f"scope must be project-relative: {scope!r}")
    if any(character in _FORBIDDEN_CHARACTERS for character in scope):
        raise DevPlaneError(f"unsupported character in scope: {scope!r}")
    segments = scope.split("/")
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment in ("", ".", ".."):
            raise DevPlaneError(f"invalid path segment in scope: {scope!r}")
        if _TREE in segment and not (segment == _TREE and index == last):
            raise DevPlaneError(f"'**' is only allowed as the final segment: {scope!r}")
    return segments


def validate_scope(scope: str) -> str:
    """Return ``scope`` unchanged if it is inside the supported grammar."""
    _split_scope(scope)
    return scope


def _split_path(path: str) -> list[str]:
    if not isinstance(path, str) or not path:
        raise DevPlaneError("path must be a non-empty string")
    if path.startswith("/") or "\\" in path:
        raise DevPlaneError(f"path must be canonical and project-relative: {path!r}")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise DevPlaneError(f"path must be canonical and project-relative: {path!r}")
    return segments


@lru_cache(maxsize=4096)
def _segment_regex(pattern: str) -> re.Pattern[str]:
    parts = (
        "[^/]*"
        if character == "*"
        else "[^/]"
        if character == "?"
        else re.escape(character)
        for character in pattern
    )
    return re.compile("".join(parts))


@lru_cache(maxsize=65536)
def _segments_intersect(first: str, second: str) -> bool:
    """Exactly decide whether some string matches both segment patterns."""

    @cache
    def walk(i: int, j: int) -> bool:
        if i == len(first) and j == len(second):
            return True
        a = first[i] if i < len(first) else None
        b = second[j] if j < len(second) else None
        if a == "*":
            return walk(i + 1, j) or (b is not None and walk(i, j + 1))
        if b == "*":
            return walk(i, j + 1) or (a is not None and walk(i + 1, j))
        if a is None or b is None:
            return False
        if a == "?" or b == "?" or a == b:
            return walk(i + 1, j + 1)
        return False

    return walk(0, 0)


def _split_tree(segments: list[str]) -> tuple[list[str], bool]:
    if segments[-1] == _TREE:
        return segments[:-1], True
    return segments, False


def path_in_scope(path: str, scope: str) -> bool:
    """Whether the concrete ``path`` is matched by ``scope``.

    ``path`` must already be canonical and project-relative; anything else
    raises :class:`DevPlaneError` so callers fail closed.
    """
    prefix, is_tree = _split_tree(_split_scope(scope))
    parts = _split_path(path)
    if is_tree:
        if len(parts) <= len(prefix):
            return False
        parts = parts[: len(prefix)]
    elif len(parts) != len(prefix):
        return False
    return all(
        _segment_regex(pattern).fullmatch(part) is not None
        for part, pattern in zip(parts, prefix, strict=True)
    )


def scopes_overlap(first: str, second: str) -> bool:
    """Whether some project path can be matched by both scopes (exact)."""
    first_prefix, first_tree = _split_tree(_split_scope(first))
    second_prefix, second_tree = _split_tree(_split_scope(second))
    shared = min(len(first_prefix), len(second_prefix))
    for index in range(shared):
        if not _segments_intersect(first_prefix[index], second_prefix[index]):
            return False
    if len(first_prefix) == len(second_prefix):
        # Equal depth: a tree scope needs at least one further segment, which a
        # non-tree scope of the same depth can never provide.
        return first_tree == second_tree
    shorter_is_tree = (
        first_tree if len(first_prefix) < len(second_prefix) else second_tree
    )
    return shorter_is_tree
