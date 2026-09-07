"""The offline baseline must never inherit a user's runtime roots."""

import asyncio
import socket
from dataclasses import FrozenInstanceError

import pytest

from nanobot.config import loader, paths
from nanobot.session.manager import SessionManager


def test_roots_are_local_and_distinct(isolated_roots, tmp_path):
    roots = isolated_roots
    assert roots.workspace.is_relative_to(tmp_path)
    assert roots.runtime.is_relative_to(tmp_path)
    assert roots.config.is_relative_to(tmp_path)
    assert roots.workspace != roots.runtime
    assert roots.config == roots.runtime / "config.json"
    assert roots.workspace.is_dir()
    assert roots.runtime.is_dir()
    assert loader.get_config_path() == roots.config
    assert paths.get_data_dir() == roots.runtime
    assert roots.config.is_file()
    with pytest.raises(FrozenInstanceError):
        roots.workspace = tmp_path


def test_session_and_legacy_targets_stay_in_test_root(isolated_roots):
    roots = isolated_roots
    manager = SessionManager(roots.workspace, sessions_root=roots.runtime / "sessions")
    assert manager.sessions_dir.is_relative_to(roots.runtime / "sessions")
    assert manager.legacy_sessions_dir == roots.runtime / "legacy-sessions"
    # Exercise the default alias too: the repository's parent fixture must not
    # leave this manager pointing at a sibling outside this test's tmp_path.
    assert SessionManager(roots.workspace).sessions_dir == manager.sessions_dir


async def test_network_is_denied(isolated_roots):
    with pytest.raises(AssertionError, match="N00 offline isolation"):
        socket.create_connection(("example.invalid", 443))
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="N00 offline isolation"):
            sock.connect(("192.0.2.1", 443))
        with pytest.raises(AssertionError, match="N00 offline isolation"):
            await asyncio.get_running_loop().sock_connect(sock, ("192.0.2.1", 443))


async def test_config_selection_is_restored_on_exception(
    isolated_roots, tmp_path, isolation_scope, monkeypatch
):
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="teardown probe"):
        with isolation_scope(nested) as roots:
            assert loader.get_config_path() == roots.config
            raise RuntimeError("teardown probe")
    assert loader.get_config_path() == isolated_roots.config
    unset = tmp_path / "previously-unset"
    unset.mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(loader, "_current_config_path", None)
        with isolation_scope(unset):
            assert loader.get_config_path().is_relative_to(unset)
        assert loader._current_config_path is None
