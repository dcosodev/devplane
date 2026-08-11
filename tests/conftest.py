import pytest


@pytest.fixture(autouse=True)
def isolated_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Git-writing tests independent from the host's global config."""
    identity = {
        "GIT_AUTHOR_NAME": "DevPlane Tests",
        "GIT_AUTHOR_EMAIL": "devplane-tests@example.invalid",
        "GIT_COMMITTER_NAME": "DevPlane Tests",
        "GIT_COMMITTER_EMAIL": "devplane-tests@example.invalid",
    }
    for name, value in identity.items():
        monkeypatch.setenv(name, value)
