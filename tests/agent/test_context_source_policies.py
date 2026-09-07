"""N08 source-policy contracts."""

from __future__ import annotations

import base64
import hashlib
import struct
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest


def _png_data(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(
        ">II", width, height
    )


def _image_block(raw: bytes, *, detail: str = "high", path: Path | None = None) -> dict:
    block = {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
            "detail": detail,
        },
    }
    if path is not None:
        block["_meta"] = {"path": str(path)}
    return block


def test_context_source_policy_protects_required_sources_and_retains_indexes() -> None:
    from nanobot.agent.context_plan import message_sources
    from nanobot.agent.context_sources import ContextSourcePolicy

    assert ContextSourcePolicy.for_source("system_policy").allowed_strategies == ("keep",)
    assert ContextSourcePolicy.for_source("skill_full").required is True
    assert ContextSourcePolicy.for_source("image", current=True).required is True
    assert ContextSourcePolicy.for_source("image").allowed_strategies == (
        "keep",
        "reference",
    )
    assert ContextSourcePolicy.for_source("skill_index").allowed_strategies == (
        "keep",
        "defer",
    )
    old_image = next(
        source
        for source in message_sources(
            "history:0",
            {"role": "user", "content": [_image_block(_png_data(32, 32))]},
        )
        if source.kind == "image"
    )
    assert old_image.required is False
    assert old_image.allowed_strategies == ("keep", "reference")


def test_image_cost_is_recalculated_for_actual_known_model_family() -> None:
    from nanobot.agent.context_sources import estimate_image_tokens

    block = _image_block(_png_data(512, 512))

    openai = estimate_image_tokens(block, "gpt-4o")
    anthropic = estimate_image_tokens(block, "claude-3-5-sonnet-20241022")

    assert (openai.tokens, openai.width, openai.height) == (255, 512, 512)
    assert (anthropic.tokens, anthropic.width, anthropic.height) == (361, 512, 512)
    assert openai.method == "openai_tiles"
    assert anthropic.method == "anthropic_patches"


@pytest.mark.parametrize(
    ("model", "detail", "expected", "method"),
    [
        ("openai/gpt-4.1", "high", 255, "openai_tiles"),
        ("openai/gpt-4o-mini", "high", 8500, "openai_tiles"),
        ("openai-codex/gpt-5.6-sol", "high", 308, "openai_patches"),
        ("openai/gpt-5.5", "original", 308, "openai_patches"),
        ("github-copilot/gpt-5.4-mini", "high", 308, "openai_patches"),
    ],
)
def test_image_cost_uses_current_model_specific_official_rules(
    model: str,
    detail: str,
    expected: int,
    method: str,
) -> None:
    from nanobot.agent.context_sources import estimate_image_tokens

    estimate = estimate_image_tokens(
        _image_block(_png_data(512, 512), detail=detail),
        model,
    )

    assert estimate.tokens == expected
    assert estimate.method == method


def test_image_cost_fails_closed_for_unknown_inputs() -> None:
    from nanobot.agent.context_sources import estimate_image_tokens

    raw = _png_data(32, 32)

    assert estimate_image_tokens(_image_block(raw), "local/unknown").tokens is None
    assert estimate_image_tokens(_image_block(raw, detail="cinematic"), "gpt-4o").tokens is None
    assert estimate_image_tokens(_image_block(raw), "gpt-4.1-nano").tokens is None
    assert estimate_image_tokens(
        {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
        "gpt-4o",
    ).tokens is None
    assert estimate_image_tokens(
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,bad"}},
        "gpt-4o",
    ).tokens is None


def test_anthropic_high_resolution_family_uses_its_documented_patch_tier() -> None:
    from nanobot.agent.context_sources import estimate_image_tokens

    estimate = estimate_image_tokens(_image_block(_png_data(2000, 2000)), "claude-sonnet-5")

    assert estimate.tokens == 4761
    assert estimate.method == "anthropic_patches"


def test_consumed_old_image_reference_requires_workspace_path_and_exact_hash(
    tmp_path: Path,
) -> None:
    from nanobot.agent.context_sources import reference_verified_images

    raw = _png_data(48, 24)
    image_path = tmp_path / "assets" / "old.png"
    image_path.parent.mkdir()
    image_path.write_bytes(raw)
    original = [{"role": "user", "content": [_image_block(raw, path=image_path)]}]

    referenced = reference_verified_images(
        original,
        verified_message_indexes={0},
        workspace=tmp_path,
    )

    replacement = referenced.messages[0]["content"][0]
    digest = hashlib.sha256(raw).hexdigest()
    assert replacement == {
        "type": "text",
        "text": (
            "[Previously consumed image: assets/old.png; "
            f"sha256={digest}. Use read_file with path 'assets/old.png' to load it again.]"
        ),
    }
    assert referenced.references[0].content_hash == digest
    assert original[0]["content"][0]["type"] == "image_url"

    without_proof = reference_verified_images(
        original,
        verified_message_indexes=set(),
        workspace=tmp_path,
    )
    assert without_proof.messages == original

    image_path.write_bytes(raw + b"changed")
    changed = reference_verified_images(
        original,
        verified_message_indexes={0},
        workspace=tmp_path,
    )
    assert changed.messages == original


def test_builder_applies_source_policies_and_include_memory_boundary(tmp_path: Path) -> None:
    from nanobot.agent.context import ContextBuilder, TranscriptInput
    from nanobot.agent.context_plan import ContextPlanner

    (tmp_path / "SOUL.md").write_text("custom soul", encoding="utf-8")
    (tmp_path / "USER.md").write_text("custom user", encoding="utf-8")
    always = tmp_path / "skills" / "always-skill"
    lazy = tmp_path / "skills" / "lazy-skill"
    always.mkdir(parents=True)
    lazy.mkdir(parents=True)
    (always / "SKILL.md").write_text(
        "---\nname: always-skill\ndescription: always\nalways: true\n---\nALWAYS BODY",
        encoding="utf-8",
    )
    (lazy / "SKILL.md").write_text(
        "---\nname: lazy-skill\ndescription: lazy\n---\nLAZY BODY",
        encoding="utf-8",
    )
    builder = ContextBuilder(tmp_path)
    builder.memory.write_memory("custom memory")
    transcript = TranscriptInput(history=[], current_message="$lazy-skill run")

    sources = builder.collect_sources(transcript)
    by_id = {source.source_id: source for source in sources}
    memory_ids = {"memory:SOUL.md", "memory:USER.md", "memory:MEMORY.md"}

    assert memory_ids <= by_id.keys()
    assert len({by_id[source_id].content_hash for source_id in memory_ids}) == 3
    assert by_id["skills:active"].required is True
    assert by_id["skills:index"].allowed_strategies == ("keep", "defer")
    assert any(source.kind == "skill_full" and source.required for source in sources)
    assert all(
        decision.action == "keep"
        for decision in ContextPlanner().plan(sources, input_budget=100_000).decisions
    )

    without_memory = builder.collect_sources(transcript, include_memory=False)
    assert not any(source.kind == "memory" for source in without_memory)


class _OfflineProvider:
    def can_resume_conversation_state(self, *_args: object, **_kwargs: object) -> bool:
        return False


def _governance_state(
    messages: list[dict],
    tmp_path: Path,
    *,
    model: str,
    compaction: object | None = None,
    enabled_layers: list[str] | None = None,
    mode: str = "enforce",
):
    from nanobot.agent.context_governance import ContextGovernanceConfig, ModelRequestState
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import ContextConfig
    from nanobot.providers.conversation_state import ProviderConversationStateController

    provider = _OfflineProvider()
    config = ContextGovernanceConfig(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        tools=ToolRegistry(),
        workspace=tmp_path,
        session_key="n08:image",
        max_tool_result_chars=16_000,
        context_window_tokens=2_000,
        max_tokens=100,
        context=ContextConfig(mode=mode, enabled_layers=enabled_layers or []),  # type: ignore[arg-type]
        runtime_data_dir=tmp_path / "runtime",
    )
    return ModelRequestState(
        config=config,
        conversation=ProviderConversationStateController(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            messages=messages,
        ),
        compaction=compaction,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_governor_uses_model_image_cost_without_counting_base64(tmp_path: Path) -> None:
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError

    raw = _png_data(512, 512) + b"x" * 100_000
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": [_image_block(raw), {"type": "text", "text": "now"}]},
    ]

    supported = _governance_state(messages, tmp_path, model="gpt-4o")
    prepared, _ = await ContextGovernor().prepare_request(
        supported,
        messages,
        tool_definitions=[],
    )
    assert prepared[1]["content"][0]["type"] == "image_url"
    image_source = next(source for source in supported.manifest.plan.sources if source.kind == "image")
    assert image_source.estimated_tokens == 255
    assert supported.manifest.plan.rendered_estimate is not None
    assert supported.manifest.plan.rendered_estimate < 1_000

    unknown = _governance_state(messages, tmp_path, model="local/unknown")
    with pytest.raises(ContextPlanError, match="unknown_image_cost"):
        await ContextGovernor().prepare_request(unknown, messages, tool_definitions=[])


@pytest.mark.asyncio
async def test_observe_manifest_reestimates_images_after_legacy_preparation(tmp_path: Path) -> None:
    from nanobot.agent.context_governance import ContextGovernor

    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": [_image_block(_png_data(512, 512))]},
    ]
    state = _governance_state(messages, tmp_path, model="gpt-4o", mode="observe")

    await ContextGovernor().prepare_request(state, messages, tool_definitions=[])

    assert state.manifest is not None
    image_source = next(source for source in state.manifest.plan.sources if source.kind == "image")
    assert image_source.estimated_tokens == 255
    assert state.manifest.plan.rendered_estimate is not None
    assert state.manifest.plan.rendered_estimate < 1_000


@pytest.mark.asyncio
async def test_l2_pressure_uses_image_cost_instead_of_base64_length(tmp_path: Path) -> None:
    from nanobot.agent.context import TranscriptInput
    from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw_image = _png_data(512, 512) + b"x" * 100_000
    raw = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "verified old turn"},
        {"role": "user", "content": [_image_block(raw_image), {"type": "text", "text": "now"}]},
    ]
    transcript = TranscriptInput(history=raw[1:2], current_message="now")
    _built, compaction = ContextCompactionState.from_transcript(
        transcript,
        lambda _transcript: raw,
        None,
        None,
        track_consumption=True,
    )
    assert compaction is not None
    request = ContextRequestOutcome("seed", "seed", 0, "unknown")
    compaction.begin_request(raw[:2], raw_boundary=2, request=request)
    compaction.accept_request(
        raw[:2],
        raw_boundary=2,
        outcome=replace(request, status="accepted"),
    )
    state = _governance_state(
        raw,
        tmp_path,
        model="gpt-4o",
        compaction=compaction,
        enabled_layers=["L2"],
    )

    prepared, _ = await ContextGovernor().prepare_request(
        state,
        raw,
        tool_definitions=[],
        transcript=raw,
    )

    assert prepared[1] == raw[1]
    assert not any(decision.stage == "L2" for decision in state.manifest.plan.decisions)


@pytest.mark.asyncio
async def test_governor_references_only_verified_old_image_and_preserves_raw(tmp_path: Path) -> None:
    from nanobot.agent.context import TranscriptInput
    from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
    from nanobot.agent.context_plan import ContextRequestOutcome, content_hash

    raw_image = _png_data(64, 32)
    image_path = tmp_path / "old.png"
    image_path.write_bytes(raw_image)
    raw = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": [_image_block(raw_image, path=image_path)]},
        {"role": "user", "content": "current"},
    ]
    original = deepcopy(raw)
    transcript = TranscriptInput(history=raw[1:2], current_message="current")
    _built, compaction = ContextCompactionState.from_transcript(
        transcript,
        lambda _transcript: raw,
        None,
        None,
        track_consumption=True,
    )
    assert compaction is not None
    request = ContextRequestOutcome("seed", "seed", 0, "unknown")
    compaction.begin_request(raw[:2], raw_boundary=2, request=request)
    compaction.accept_request(
        raw[:2],
        raw_boundary=2,
        outcome=replace(request, status="accepted"),
    )
    state = _governance_state(raw, tmp_path, model="local/unknown", compaction=compaction)

    prepared, _ = await ContextGovernor().prepare_request(
        state,
        raw,
        tool_definitions=[],
        transcript=raw,
    )

    assert "Use read_file" in prepared[1]["content"][0]["text"]
    assert not any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for message in prepared
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
    )
    assert state.manifest.reason_code == "image_reference"
    assert any(
        decision.action == "reference" and decision.reason_code == "image_reference"
        for decision in state.manifest.plan.decisions
    )
    assert raw == original
    assert tuple(content_hash(message) for message in raw[:2]) == compaction.accepted_raw_hashes


def test_schema_selection_never_truncates_parameters() -> None:
    from nanobot.agent.context_sources import select_tool_schemas

    definitions = [
        {
            "type": "function",
            "function": {
                "name": "mcp_a",
                "parameters": {
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"enum": ["first", "second"]}},
                },
            },
        },
        {
            "type": "function",
            "function": {"name": "mcp_b", "parameters": {"type": "object"}},
        },
    ]

    selected = select_tool_schemas(definitions, {"mcp_a"}, discovery_enabled=True)

    assert selected.definitions == definitions[:1]
    assert selected.deferred_names == ("mcp_b",)


def test_schema_selection_disabled_preserves_complete_catalog() -> None:
    from nanobot.agent.context_sources import select_tool_schemas

    definitions = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    selected = select_tool_schemas(definitions, set(), discovery_enabled=False)

    assert selected.definitions == definitions
    assert selected.deferred_names == ()
