from pathlib import Path

from test_sync import make_project

from devplane.context import build_context_bundle


def test_context_bundle_explains_inclusions(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    bundle = build_context_bundle(project, "plan")
    assert "Command: plan" in bundle
    assert "corp-base/instructions.md" in bundle
    assert "capability corp-base" in bundle
    assert "Write scopes" in bundle
