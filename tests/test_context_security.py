from pathlib import Path

import pytest

from devplane import context
from devplane.core import DevPlaneError


@pytest.mark.parametrize("command", ["../../outside", "../plan", "/tmp/file", "plan/extra", "Plan", ""])
def test_write_context_rejects_unsafe_command_names(tmp_path: Path, command: str) -> None:
    with pytest.raises(DevPlaneError, match="invalid context command"):
        context.write_context_bundle(tmp_path, command)

    assert not (tmp_path.parent / "outside.md").exists()


def test_write_context_accepts_safe_command_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(context, "build_context_bundle", lambda project, command: "safe\n")

    output = context.write_context_bundle(tmp_path, "parallel-implement")

    assert output == tmp_path / ".devplane" / "generated" / "context-parallel-implement.md"
    assert output.read_text(encoding="utf-8") == "safe\n"
