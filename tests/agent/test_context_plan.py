"""N05 source selection contracts."""

from dataclasses import FrozenInstanceError

import pytest


def test_required_source_kept():
    from nanobot.agent.context_plan import ContextPlanner, ContextSource

    source = ContextSource(
        source_id="current:1", kind="user_current", revision="1", content_hash=None,
        estimated_tokens=10, estimate_method="fixture", priority=100,
        required=True, allowed_strategies=("keep",), render_key="current:1",
    )
    plan = ContextPlanner().plan([source], input_budget=100)
    assert plan.decisions[0].action == "keep"
    assert plan.decisions[0].reason_code == "kept_required"
    assert plan.predicted_total == 10
    assert plan.input_budget == 100
    with pytest.raises(FrozenInstanceError):
        source.required = False


def test_invalid_sources_and_budget_are_rejected():
    from nanobot.agent.context_plan import ContextPlanner, ContextSource

    values = dict(source_id="a", kind="history", revision="1", content_hash=None,
                  estimated_tokens=10, estimate_method="fixture", priority=10,
                  required=False, allowed_strategies=("keep",), render_key="a")
    with pytest.raises(ValueError, match="revision"):
        ContextSource(**(values | {"revision": None}))
    source = ContextSource(**values)
    with pytest.raises(ValueError, match="unique"):
        ContextPlanner().plan([source, source], 100)
    with pytest.raises(ValueError, match="positive"):
        ContextPlanner().plan([source], 0)


def test_catalog_precedes_whole_render_and_is_plan_local(tmp_path, monkeypatch):
    from nanobot.agent.context import ContextBuilder, TranscriptInput
    from nanobot.agent.context_plan import ContextPlanner

    builder = ContextBuilder(tmp_path)
    (tmp_path / "USER.md").write_text("private-user-A", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("private-soul-A", encoding="utf-8")
    builder.memory.write_memory("private-memory-A")
    transcript = TranscriptInput(history=[], current_message="current-A")
    expected = builder.build_transcript(transcript)
    monkeypatch.setattr(builder, "build_system_prompt", lambda **kw: pytest.fail("whole render"))
    sources = builder.collect_sources(transcript)
    first = ContextPlanner().plan(sources, 100000)
    (tmp_path / "USER.md").write_text("private-user-B", encoding="utf-8")
    second = ContextPlanner().plan(builder.collect_sources(
        TranscriptInput(history=[], current_message="current-B")), 100000)
    assert builder.render_plan(first).messages == expected
    assert "current-B" == builder.render_plan(second).messages[-1]["content"]
    assert len({s.source_id for s in sources}) == len(sources)
    assert {"memory:USER.md", "memory:SOUL.md", "memory:MEMORY.md"} <= {
        s.source_id for s in sources if s.kind == "memory"
    }
    assert "private-user-A" not in repr(first)


def test_unknown_image_cost_is_not_zero(tmp_path):
    from nanobot.agent.context import ContextBuilder, TranscriptInput
    from nanobot.agent.context_plan import ContextPlanner

    sources = ContextBuilder(tmp_path).collect_sources(TranscriptInput(
        history=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
            {"type": "text", "text": "describe"},
        ]}], current_message="now"))
    images = [s for s in sources if s.kind == "image"]
    assert len(images) == 1
    assert images[0].estimated_tokens is None
    assert ContextPlanner().plan(sources, 10000).predicted_total is None


def test_explicit_skill_and_runtime_are_separate_sources(tmp_path):
    from nanobot.agent.context import ContextBuilder, TranscriptInput
    from nanobot.runtime_context import RuntimeContextBlock

    skill_dir = tmp_path / "skills" / "n05-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: n05-skill\ndescription: private skill\n---\nprivate-skill-body", encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    sources = builder.collect_sources(TranscriptInput(
        history=[], current_message="$n05-skill please",
        runtime_context_blocks=[RuntimeContextBlock(source="clock", content="private-runtime")]))
    current = [s for s in sources if s.source_id.startswith("current:")]
    assert {s.kind for s in current} == {"user_current", "skill_full", "runtime"}
    assert all(s.required for s in current)
