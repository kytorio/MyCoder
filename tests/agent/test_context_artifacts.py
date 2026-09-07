"""Focused N06 artifact persistence and scoped read-tool contracts."""

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        # Windows asyncio's socketpair uses loopback internally.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect(sock, address)
        raise OSError("Network disabled for artifact tests")

    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect_ex(sock, address)
        raise OSError("Network disabled for artifact tests")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


def test_exact_roundtrip_and_pages_after_restart(tmp_path: Path) -> None:
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    original = "中文🙂e\u0301\r\nline\n\r\x00" * 21
    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("session:a", "call:1", original)
    restarted = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    assert restarted.read(ref, "session:a", 100_000) == original
    assert ref.byte_length == len(original.encode("utf-8"))
    cursor = 0
    chunks: list[str] = []
    while True:
        page = restarted.read_page(ref, "session:a", offset_chars=cursor, max_chars=7)
        assert page.content_hash == ref.content_hash
        assert len(page.text) <= 7
        chunks.append(page.text)
        if page.next_offset_chars is None:
            break
        assert page.next_offset_chars == cursor + len(page.text)
        cursor = page.next_offset_chars
    assert "".join(chunks) == original


@pytest.mark.parametrize("change,scope", [
    ({}, "session:b"),
    ({"scope": "session:b"}, "session:b"),
    ({"content_hash": "0" * 64}, "session:a"),
    ({"byte_length": 0}, "session:a"),
    ({"artifact_id": "../secret"}, "session:a"),
    ({"artifact_id": "C:\\secret"}, "session:a"),
    ({"artifact_id": "a" * 64 + ":stream"}, "session:a"),
])
def test_scope_and_reference_forgery_denied(tmp_path, change, scope):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("session:a", "call:1", "private text")
    with pytest.raises(PermissionError):
        store.read(replace(ref, **change), scope, 100_000)


def test_resolve_and_idempotent_versions_do_not_rewrite(tmp_path, monkeypatch):
    import nanobot.agent.context_artifacts as artifacts

    store = artifacts.ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("session:a", "call:1", "old")
    newer = store.put("session:a", "call:1", "new")
    assert newer.artifact_id != ref.artifact_id
    assert store.read(ref, "session:a", 100_000) == "old"
    assert store.read(newer, "session:a", 100_000) == "new"

    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(artifacts, "write_text_atomic", fail_write)
    restarted = artifacts.ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    assert restarted.put("session:a", "call:1", "old") == ref
    assert restarted.resolve(ref.artifact_id, "session:a") == ref
    with pytest.raises(PermissionError):
        restarted.resolve(ref.artifact_id, "session:b")


@pytest.mark.parametrize("operation", ["read_limit", "negative_offset", "past_end", "zero_page"])
def test_read_limits_are_explicit(tmp_path, operation):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("session:a", "call:1", "中")
    with pytest.raises(ValueError):
        if operation == "read_limit":
            store.read(ref, "session:a", 2)
        else:
            store.read_page(ref, "session:a", offset_chars={"negative_offset": -1, "past_end": 2}.get(operation, 0), max_chars=0 if operation == "zero_page" else 1)
    assert store.read(ref, "session:a", 3) == "中"
    assert store.read_page(ref, "session:a", offset_chars=1).next_offset_chars is None


def test_capacity_exact_boundary_and_prewrite_failure(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    probe = tmp_path / "probe"
    ToolResultArtifactStore(probe, max_bytes=100_000).put("s", "c", "中\r\n")
    record_bytes = sum(p.stat().st_size for p in probe.iterdir())
    assert record_bytes > len("中\r\n".encode())
    root = tmp_path / "exact"
    store = ToolResultArtifactStore(root, max_bytes=record_bytes)
    ref = store.put("s", "c", "中\r\n")
    assert sum(p.stat().st_size for p in root.iterdir()) == record_bytes
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(OSError):
        store.put("s", "d", "中\r\n")
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert store.put("s", "c", "中\r\n") == ref
    tiny = ToolResultArtifactStore(tmp_path / "tiny", max_bytes=record_bytes - 1)
    with pytest.raises(OSError):
        tiny.put("s", "c", "中\r\n")
    assert not list(tiny.root.iterdir())


def test_same_root_stores_coordinate_capacity(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    probe = tmp_path / "probe"
    ToolResultArtifactStore(probe, max_bytes=100_000).put("s", "c", "one")
    capacity = sum(p.stat().st_size for p in probe.iterdir())
    root = tmp_path / "shared"
    stores = [ToolResultArtifactStore(root, max_bytes=capacity) for _ in range(12)]

    def write_one(index):
        try:
            return stores[index].put("s", str(index), "one")
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(write_one, range(12)))
    assert sum(ref is not None for ref in results) == 1
    assert sum(p.stat().st_size for p in root.iterdir()) == capacity


@pytest.mark.parametrize("mutation", ["text", "scope", "content_hash", "version", "truncated"])
def test_every_read_rechecks_persisted_integrity(tmp_path, mutation):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("s", "c", "private")
    path = next(tmp_path.iterdir())
    record = json.loads(path.read_bytes())
    if mutation == "truncated":
        damaged = b'{"text":'
    else:
        record[mutation] = {"text": "altered", "scope": "other", "content_hash": "0" * 64, "version": 2}[mutation]
        damaged = json.dumps(record).encode()
    path.write_bytes(damaged)
    restarted = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    for instance in (store, restarted):
        with pytest.raises((ValueError, PermissionError)):
            instance.read(ref, "s", 100_000)
        with pytest.raises((ValueError, PermissionError)):
            instance.resolve(ref.artifact_id, "s")
        with pytest.raises((ValueError, PermissionError)):
            instance.put("s", "c", "private")
    assert path.read_bytes() == damaged


@pytest.mark.parametrize("failure", ["raise", "partial_return", "corrupt_after_write"])
def test_atomic_failure_never_returns_valid_reference(tmp_path, monkeypatch, failure):
    import nanobot.agent.context_artifacts as artifacts

    store = artifacts.ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    good = store.put("s", "good", "preserved")
    real_write = artifacts.write_text_atomic
    attempted = []

    def broken_write(path, content, *, newline=None):
        attempted.append(path)
        if failure == "raise":
            raise OSError("private disk failure")
        if failure == "corrupt_after_write":
            real_write(path, content, newline=newline)
        path.write_bytes(b'{"broken":')

    monkeypatch.setattr(artifacts, "write_text_atomic", broken_write)
    with pytest.raises((OSError, ValueError)):
        store.put("s", "broken", "never issued")
    assert store.read(good, "s", 100_000) == "preserved"
    if failure != "raise":
        assert attempted[0].read_bytes() == b'{"broken":'
        with pytest.raises((OSError, ValueError)):
            store.resolve(attempted[0].stem, "s")


def _directory_link(link, target, kind):
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junctions unavailable")
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
            + "' -Target '" + str(target).replace("'", "''") + "' -ErrorAction Stop | Out-Null",
        ], capture_output=True)
        if result.returncode:
            pytest.skip("Junction creation unavailable")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("Symlink creation unavailable")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_linked_root_and_ancestor_denied(tmp_path, kind):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    _directory_link(link, outside, kind)
    for root in (link, link / "child"):
        with pytest.raises(PermissionError):
            ToolResultArtifactStore(root, max_bytes=100_000)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_replaced_root_denies_existing_store_reads_and_writes(tmp_path, kind):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    root = tmp_path / "root"
    store = ToolResultArtifactStore(root, max_bytes=100_000)
    ref = store.put("s", "c", "private")
    moved = tmp_path / "moved"
    root.rename(moved)
    _directory_link(root, moved, kind)
    with pytest.raises(PermissionError):
        store.read(ref, "s", 100_000)
    with pytest.raises(PermissionError):
        store.put("s", "new", "no write")
    assert len(list(moved.iterdir())) == 1


def test_artifact_file_link_denied(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    root = tmp_path / "root"
    store = ToolResultArtifactStore(root, max_bytes=100_000)
    ref = store.put("s", "c", "private")
    path = next(root.iterdir())
    moved = tmp_path / "original.json"
    path.rename(moved)
    try:
        path.symlink_to(moved)
    except OSError:
        pytest.skip("File symlinks unavailable")
    with pytest.raises(PermissionError):
        store.read(ref, "s", 100_000)


def test_quota_counts_orphan_and_nested_files_without_following_links(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    nested = tmp_path / "evidence"
    nested.mkdir()
    (nested / "partial.tmp").write_bytes(b"x" * 1000)
    store = ToolResultArtifactStore(tmp_path, max_bytes=1000)
    with pytest.raises(OSError):
        store.put("s", "c", "x")
    assert (nested / "partial.tmp").read_bytes() == b"x" * 1000


def test_root_traversal_and_invalid_capacity_denied_before_creation(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    with pytest.raises(PermissionError):
        ToolResultArtifactStore(tmp_path / "nested" / ".." / "escaped", max_bytes=1000)
    for capacity in (0, -1, True):
        with pytest.raises(ValueError):
            ToolResultArtifactStore(tmp_path / "invalid", max_bytes=capacity)
    assert not list(tmp_path.iterdir())


async def test_tool_reconstructs_all_unicode_pages_under_small_budget(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool
    from nanobot.utils.helpers import estimate_prompt_tokens

    original = '中🙂e\u0301\r\n"\\\t' * 300
    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("s", "c", original)
    tool = ContextArtifactTool(store, page_token_budget=384)
    cursor = 0
    chunks = []
    with request_context(RequestContext(channel="test", chat_id="a", session_key="s")):
        for _ in range(len(original) + 1):
            output = await tool.execute(ref=ref.artifact_id, offset_chars=cursor, max_chars=16384)
            assert not output.is_error, output
            assert estimate_prompt_tokens([{"role": "tool", "name": tool.name, "content": output}]) - 4 <= 384
            value = json.loads(output)
            assert set(value) == {"artifact_id", "offset_chars", "text", "next_offset_chars", "content_hash"}
            assert value["artifact_id"] == ref.artifact_id
            assert value["content_hash"] == ref.content_hash
            assert value["offset_chars"] == cursor
            chunks.append(value["text"])
            if value["next_offset_chars"] is None:
                break
            assert value["text"]
            assert value["next_offset_chars"] == cursor + len(value["text"])
            cursor = value["next_offset_chars"]
        else:
            pytest.fail("Pagination did not finish")
    assert len(chunks) > 1
    assert "".join(chunks) == original
    assert len(list(tmp_path.iterdir())) == 1


async def test_tool_unicode_pagination_with_byte_estimator_fallback(tmp_path, monkeypatch):
    import nanobot.utils.helpers as helpers

    def no_encoder():
        raise OSError("Tokenizer unavailable offline")

    monkeypatch.setattr(helpers, "_get_token_encoding", no_encoder)
    await test_tool_reconstructs_all_unicode_pages_under_small_budget(tmp_path)


def test_store_preserves_arbitrary_python_string_codepoints(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    original = "paired:\ud83d\ude42; lone:\ud800; scalar:🙂\r\n"
    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("s", "c", original)
    restarted = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    assert restarted.read(ref, "s", 100_000) == original


async def test_tool_missing_scope_and_bad_arguments_are_content_free(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("s", "c", "private content")
    tool = ContextArtifactTool(store)
    outputs = [await tool.execute(ref=ref.artifact_id)]
    with request_context(RequestContext(channel="test", chat_id="a", session_key="other")):
        outputs.append(await tool.execute(ref=ref.artifact_id))
    with request_context(RequestContext(channel="test", chat_id="a", session_key="s")):
        for params in ({"ref": "../secret"}, {"ref": ref.artifact_id, "scope": "s"},
                       {"ref": ref.artifact_id, "path": str(tmp_path)},
                       {"ref": ref.artifact_id, "max_chars": 0},
                       {"ref": ref.artifact_id, "max_chars": 16385},
                       {"ref": ref.artifact_id, "offset_chars": -1},
                       {"ref": ref.artifact_id, "offset_chars": True}):
            outputs.append(await tool.execute(**params))
    for output in outputs:
        assert output.is_error
        assert len(output) < 160
        assert str(tmp_path) not in output
        assert "private" not in output
        assert "secret" not in output


async def test_real_loader_registration_and_schema(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import ToolsConfig
    from nanobot.utils.helpers import estimate_prompt_tokens

    loader = ToolLoader(test_classes=[ContextArtifactTool])
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    registry = ToolRegistry()
    assert loader.load(ctx, registry) == []
    with pytest.raises(ValueError):
        ContextArtifactTool.create(ctx)
    store = ToolResultArtifactStore(tmp_path / "artifacts", max_bytes=100_000)
    ctx.context_artifact_store = store
    ctx.context_artifact_page_token_budget = 384
    ref = store.put("s", "c", "read-only text " * 300)
    assert loader.load(ctx, registry) == ["context_artifact_read"]
    assert registry.tool_names == ["context_artifact_read"]
    tool = registry.get("context_artifact_read")
    assert tool.read_only
    schema = tool.to_schema()["function"]["parameters"]
    assert set(schema["properties"]) == {"ref", "offset_chars", "max_chars"}
    assert schema["additionalProperties"] is False
    assert tool.validate_params({"ref": ref.artifact_id, "offset_chars": 0, "max_chars": 1}) == []
    for params in ({}, {"ref": ref.artifact_id, "max_chars": 0},
                   {"ref": ref.artifact_id, "max_chars": 16385},
                   {"ref": ref.artifact_id, "offset_chars": -1},
                   {"ref": ref.artifact_id, "scope": "s"}):
        assert tool.validate_params(params)
    with request_context(RequestContext(channel="test", chat_id="a", session_key="s")):
        output = await registry.execute("context_artifact_read", {"ref": ref.artifact_id})
    assert not output.is_error
    assert json.loads(output)["text"]
    assert estimate_prompt_tokens([{"role": "tool", "name": tool.name, "content": output}]) - 4 <= 384


def test_missing_store_scope_rejected_without_persisting(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    with pytest.raises(PermissionError):
        store.put("", "c", "private")
    assert not list(tmp_path.iterdir())


async def test_tool_contains_unexpected_storage_errors(tmp_path, monkeypatch):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    ref = store.put("s", "c", "private")

    def fail_resolve(*args, **kwargs):
        raise RuntimeError("private backend failure at " + str(tmp_path))

    monkeypatch.setattr(store, "resolve", fail_resolve)
    with request_context(RequestContext(channel="test", chat_id="a", session_key="s")):
        result = await ContextArtifactTool(store).execute(ref=ref.artifact_id)
    assert result.is_error
    assert len(result) < 160
    assert "private" not in result
    assert str(tmp_path) not in result


async def test_tool_empty_eof_and_impossibly_small_budget(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool

    store = ToolResultArtifactStore(tmp_path, max_bytes=100_000)
    empty = store.put("s", "empty", "")
    full = store.put("s", "full", "中🙂\r\n")
    with request_context(RequestContext(channel="test", chat_id="a", session_key="s")):
        tool = ContextArtifactTool(store)
        output = await tool.execute(ref=empty.artifact_id)
        assert not output.is_error
        assert json.loads(output)["text"] == ""
        assert json.loads(output)["next_offset_chars"] is None
        output = await tool.execute(ref=full.artifact_id, offset_chars=4)
        assert not output.is_error
        assert json.loads(output)["text"] == ""
        assert json.loads(output)["next_offset_chars"] is None
        assert (await tool.execute(ref=full.artifact_id, offset_chars=5)).is_error
        tiny = ContextArtifactTool(store, page_token_budget=1)
        assert (await tiny.execute(ref=full.artifact_id)).is_error


def test_same_root_concurrent_idempotency_and_scope_identity(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    stores = [ToolResultArtifactStore(tmp_path, max_bytes=100_000) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda store: store.put("s", "c", "中🙂\r\n"), stores))
    assert len(set(refs)) == 1
    assert len(list(tmp_path.iterdir())) == 1
    other = stores[0].put("other", "c", "中🙂\r\n")
    assert other.artifact_id != refs[0].artifact_id
    another_call = stores[0].put("s", "another", "中🙂\r\n")
    assert another_call.artifact_id != refs[0].artifact_id


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_capacity_scan_denies_nested_links(tmp_path, kind):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = ToolResultArtifactStore(root, max_bytes=100_000)
    _directory_link(root / "linked", outside, kind)
    with pytest.raises(PermissionError):
        store.put("s", "c", "private")
    assert not list(outside.iterdir())
