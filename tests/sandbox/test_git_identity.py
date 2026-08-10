"""OPE-109: sandbox git identity setup also defaults pulls to rebase."""

from unittest.mock import MagicMock

from agent.server import _configure_git_identity


async def test_configure_git_identity_sets_identity_and_rebase_pulls():
    backend = MagicMock()

    await _configure_git_identity(backend)

    (command,) = backend.execute.call_args.args
    assert "git config --global user.name" in command
    assert "git config --global user.email" in command
    assert "git config --global pull.rebase true" in command
