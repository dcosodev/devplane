"""Tests for the bounded write-scope glob module.

Scopes use a deliberately small grammar so that membership and overlap can be
decided exactly:

* literal path segments;
* ``*`` (any run of characters) and ``?`` (one character) inside a segment;
* ``**`` only as the final segment, matching one or more further segments.

Everything else is rejected up front rather than interpreted loosely.
"""

from __future__ import annotations

import fnmatch
import itertools
import random

import pytest

from devplane.core import DevPlaneError
from devplane.scopes import path_in_scope, scopes_overlap, validate_scope

# ---------------------------------------------------------------------------
# validate_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    ["src/auth/**", "**", "src/*.py", "a/b?/c", "README.md", "src/*/models.py"],
)
def test_validate_scope_accepts_supported_syntax(scope: str) -> None:
    assert validate_scope(scope) == scope


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "/abs/path",
        "../escape",
        "src/../escape",
        "src//double",
        "src/./dot",
        "src/",
        "src\\windows",
        "src/[ab].py",
        "src/{a,b}.py",
        "!src/a.py",
        "src/**/x.py",
        "**/x.py",
        "src/a**",
        "src/**/",
    ],
)
def test_validate_scope_rejects_unsupported_syntax(scope: str) -> None:
    with pytest.raises(DevPlaneError):
        validate_scope(scope)


def test_validate_scope_rejects_non_strings() -> None:
    with pytest.raises(DevPlaneError):
        validate_scope(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# path_in_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "scope", "expected"),
    [
        ("src/a.py", "src/a.py", True),
        ("src/b.py", "src/a.py", False),
        ("src/a.py", "src/*.py", True),
        ("src/x/a.py", "src/*.py", False),
        ("t/a1", "t/a?", True),
        ("t/a12", "t/a?", False),
        ("src/auth/x.py", "src/auth/**", True),
        ("src/auth/a/b.py", "src/auth/**", True),
        ("src/auth", "src/auth/**", False),
        ("src/authz/x.py", "src/auth/**", False),
        ("a", "**", True),
        ("a/b/c", "**", True),
        ("SRC/a", "src/a", False),
    ],
)
def test_path_in_scope(path: str, scope: str, expected: bool) -> None:
    assert path_in_scope(path, scope) is expected


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../x", "a/../b", "a//b", "a/./b", "a\\b", "a/"],
)
def test_path_in_scope_rejects_non_canonical_paths(path: str) -> None:
    with pytest.raises(DevPlaneError):
        path_in_scope(path, "**")


def test_path_in_scope_rejects_invalid_scope() -> None:
    with pytest.raises(DevPlaneError):
        path_in_scope("a", "src/[ab]")


# ---------------------------------------------------------------------------
# scopes_overlap: explicit cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/b.py", False),
        ("src/**", "src/auth/x.py", True),
        ("src/auth/**", "src/billing/**", False),
        ("src/auth/**", "src/**", True),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/a.txt", False),
        ("src/*.py", "src/*.txt", False),
        ("src/a*", "src/*b", True),
        ("src/auth/**", "src/auth", False),
        ("src/auth/**", "src/*", False),
        ("src/auth/**", "src/*/x.py", True),
        ("**", "anything/here", True),
        ("**", "**", True),
        ("src/a?", "src/?b", True),
        ("src/a?", "src/b?", False),
    ],
)
def test_scopes_overlap_cases(a: str, b: str, expected: bool) -> None:
    assert scopes_overlap(a, b) is expected
    assert scopes_overlap(b, a) is expected


def test_scopes_overlap_rejects_invalid_scopes() -> None:
    with pytest.raises(DevPlaneError):
        scopes_overlap("src/[ab]", "src/a")
    with pytest.raises(DevPlaneError):
        scopes_overlap("src/a", "../x")


# ---------------------------------------------------------------------------
# scopes_overlap: exhaustive check against an independent reference matcher
# ---------------------------------------------------------------------------

_NAMES = [
    "".join(chars)
    for length in (1, 2, 3)
    for chars in itertools.product("ab", repeat=length)
]
_SEGMENT_POOL = ["a", "b", "ab", "*", "?", "a*", "*b", "a?", "??", "*a*"]


def _universe(depth: int) -> list[str]:
    paths: list[str] = []
    for size in range(1, depth + 1):
        paths.extend(
            "/".join(parts) for parts in itertools.product(_NAMES, repeat=size)
        )
    return paths


def _reference_match(path: str, scope: str) -> bool:
    """Independent reference built on fnmatch, not on the module under test."""
    path_parts = path.split("/")
    scope_parts = scope.split("/")
    if scope_parts[-1] == "**":
        prefix = scope_parts[:-1]
        if len(path_parts) <= len(prefix):
            return False
        path_parts = path_parts[: len(prefix)]
        scope_parts = prefix
    if len(path_parts) != len(scope_parts):
        return False
    return all(
        fnmatch.fnmatchcase(part, pattern)
        for part, pattern in zip(path_parts, scope_parts, strict=True)
    )


def _random_scope(rng: random.Random) -> str:
    segments = [rng.choice(_SEGMENT_POOL) for _ in range(rng.randint(0, 2))]
    if not segments or rng.random() < 0.4:
        segments.append("**")
    return "/".join(segments)


def test_scopes_overlap_matches_brute_force_reference() -> None:
    """Never claim disjoint when a shared path exists, and never claim overlap
    when none exists in a universe large enough to witness every case here."""
    universe = _universe(3)
    rng = random.Random(20260804)
    for _ in range(250):
        a, b = _random_scope(rng), _random_scope(rng)
        shared = any(
            _reference_match(path, a) and _reference_match(path, b) for path in universe
        )
        assert scopes_overlap(a, b) is shared, (a, b, shared)
        assert scopes_overlap(b, a) is shared, (b, a, shared)


def test_path_in_scope_matches_reference_matcher() -> None:
    universe = _universe(3)
    rng = random.Random(7)
    for _ in range(60):
        scope = _random_scope(rng)
        for path in universe:
            assert path_in_scope(path, scope) is _reference_match(path, scope), (
                path,
                scope,
            )
