"""Policies for non-text context sources and model-facing tool schemas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nanobot.agent.context_plan import ContextDecision, ContextPlan, ContextSource


@dataclass(frozen=True, slots=True)
class ContextSourcePolicy:
    """Selection rules for one source kind."""

    priority: int
    required: bool
    allowed_strategies: tuple[str, ...]

    @classmethod
    def for_source(
        cls,
        kind: str,
        *,
        current: bool = False,
        protected: bool = False,
    ) -> ContextSourcePolicy:
        if kind in {"system_policy", "user_current", "envelope"}:
            return cls(100, True, ("keep",))
        if kind == "skill_full" or protected:
            return cls(100, True, ("keep",))
        if kind == "image":
            return cls(100 if current else 50, current, ("keep",) if current else ("keep", "reference"))
        if kind == "tool_result":
            return cls(95 if current else 50, current, ("keep", "reference"))
        if current:
            return cls(95, True, ("keep",))
        return cls(50, False, ("keep", "defer"))


@dataclass(frozen=True, slots=True)
class SchemaSelection:
    definitions: list[dict[str, Any]]
    deferred_names: tuple[str, ...]


def schema_name(definition: dict[str, Any]) -> str:
    function = definition.get("function")
    if isinstance(function, dict):
        name = cast(dict[str, Any], function).get("name")
        return name if isinstance(name, str) else ""
    name = definition.get("name")
    return name if isinstance(name, str) else ""


def select_tool_schemas(
    definitions: list[dict[str, Any]],
    loaded_names: set[str],
    *,
    discovery_enabled: bool,
) -> SchemaSelection:
    """Select whole schemas without changing the registry or schema contents."""
    if not discovery_enabled:
        return SchemaSelection(definitions=list(definitions), deferred_names=())
    selected: list[dict[str, Any]] = []
    deferred: list[str] = []
    for definition in definitions:
        name = schema_name(definition)
        if name in loaded_names:
            selected.append(definition)
        elif name:
            deferred.append(name)
    return SchemaSelection(definitions=selected, deferred_names=tuple(deferred))


@dataclass(frozen=True, slots=True)
class ImageTokenEstimate:
    tokens: int | None
    method: str
    width: int | None = None
    height: int | None = None
    content_hash: str | None = None


def _image_payload(block: dict[str, Any]) -> tuple[bytes, str] | None:
    image = block.get("image_url")
    if isinstance(image, dict):
        image_data = cast(dict[str, Any], image)
        url = image_data.get("url")
        detail = image_data.get("detail", block.get("detail", "high"))
    else:
        url = image
        detail = block.get("detail", "high")
    if not isinstance(url, str) or not isinstance(detail, str):
        return None
    if detail not in {"low", "high", "auto", "original"} or not url.startswith("data:"):
        return None
    header, separator, encoded = url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    mime = header[5:].split(";", 1)[0].lower()
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    return raw, detail


def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _gif_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 10 or raw[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")


_JPEG_SIZE_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 3 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            return None
        marker = raw[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA or offset + 2 > len(raw):
            return None
        segment_length = int.from_bytes(raw[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(raw):
            return None
        if marker in _JPEG_SIZE_MARKERS:
            if segment_length < 7:
                return None
            height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
            width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 30 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        return width, height
    if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and raw[20] == 0x2F:
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def image_dimensions(raw: bytes) -> tuple[int, int] | None:
    for parser in (_png_dimensions, _jpeg_dimensions, _gif_dimensions, _webp_dimensions):
        dimensions = parser(raw)
        if dimensions is not None and dimensions[0] > 0 and dimensions[1] > 0:
            return dimensions
    return None


def _openai_tile_tokens(
    width: int,
    height: int,
    detail: str,
    *,
    base_tokens: int,
    tile_tokens: int,
) -> int | None:
    """Apply the documented 512px tile policy for one model family."""
    if detail == "low":
        return base_tokens
    if detail == "original":
        return None
    scale = min(1.0, 2048 / max(width, height))
    width = max(1, math.floor(width * scale))
    height = max(1, math.floor(height * scale))
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width = max(1, math.floor(width * scale))
        height = max(1, math.floor(height * scale))
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return base_tokens + tile_tokens * tiles


def _resize_long_edge(width: int, height: int, maximum: int) -> tuple[int, int]:
    scale = min(1.0, maximum / max(width, height))
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def _patch_count(width: int, height: int) -> int:
    return math.ceil(width / 32) * math.ceil(height / 32)


def _fit_patch_budget(width: int, height: int, budget: int) -> tuple[int, int]:
    """Shrink using the documented 32px-patch adjustment formula."""
    if _patch_count(width, height) <= budget:
        return width, height
    shrink = math.sqrt((32**2 * budget) / (width * height))
    resized_width = width * shrink
    resized_height = height * shrink
    width_patches = resized_width / 32
    height_patches = resized_height / 32
    adjustment = min(
        math.floor(width_patches) / width_patches,
        math.floor(height_patches) / height_patches,
    )
    return (
        max(1, math.floor(resized_width * adjustment)),
        max(1, math.floor(resized_height * adjustment)),
    )


def _openai_patch_tokens(
    width: int,
    height: int,
    *,
    maximum: int,
    patch_budget: int,
    multiplier: float,
    reject_over_budget: bool = False,
) -> int | None:
    width, height = _resize_long_edge(width, height, maximum)
    patches = _patch_count(width, height)
    if patches > patch_budget:
        if reject_over_budget:
            return None
        width, height = _fit_patch_budget(width, height, patch_budget)
        patches = _patch_count(width, height)
    if patches > min(patch_budget, 30_000):
        return None
    return math.ceil(patches * multiplier)


def _openai_patch_policy(model: str, detail: str) -> tuple[int, int, float, bool] | None:
    """Return max edge, patch budget, multiplier and strict-budget behavior."""
    if model.startswith("gpt-5.6-"):
        effective = "original" if detail == "auto" else detail
        if effective == "low":
            return 512, 30_000, 1.2, False
        if effective == "high":
            return 2048, 2500, 1.2, False
        if effective == "original":
            return 65_535, 30_000, 1.2, True
        return None
    if model.startswith("gpt-5.5"):
        effective = "original" if detail == "auto" else detail
        if effective == "low":
            return 512, 30_000, 1.2, False
        if effective == "high":
            return 2048, 2500, 1.2, False
        if effective == "original":
            return 6000, 10_000, 1.2, False
        return None
    if model.startswith(("gpt-5.4",)):
        effective = "high" if detail == "auto" else detail
        if effective == "low":
            return 2048, 6144, 1.2, False
        if effective == "high":
            return 2048, 2500, 1.2, False
        if effective == "original":
            return 6000, 10_000, 1.2, False
        return None
    if model.startswith("gpt-5.2"):
        if detail == "original":
            return None
        return 2048, 6144, 1.2, False
    if model.startswith("gpt-4.1-mini"):
        if detail == "original":
            return None
        return 2048, 6144, 1.62, False
    return None


def _anthropic_patch_tokens(width: int, height: int, model: str) -> int:
    high_resolution = model.startswith(
        (
            "claude-opus-4-7",
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
        )
    )
    maximum = 2576 if high_resolution else 1568
    budget = 4784 if high_resolution else 1568
    width, height = _resize_long_edge(width, height, maximum)

    def patches(scale: float) -> int:
        scaled_width = max(1, math.floor(width * scale))
        scaled_height = max(1, math.floor(height * scale))
        return math.ceil(scaled_width / 28) * math.ceil(scaled_height / 28)

    if patches(1.0) <= budget:
        return patches(1.0)
    low, high = 0.0, 1.0
    for _ in range(48):
        middle = (low + high) / 2
        if patches(middle) <= budget:
            low = middle
        else:
            high = middle
    return patches(low)


def estimate_image_tokens(block: dict[str, Any], model: str | None) -> ImageTokenEstimate:
    """Estimate one inline image only for model families with documented formulas."""
    payload = _image_payload(block)
    if payload is None:
        return ImageTokenEstimate(None, "unknown_image")
    raw, detail = payload
    dimensions = image_dimensions(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if dimensions is None:
        return ImageTokenEstimate(None, "unknown_image", content_hash=digest)
    width, height = dimensions
    normalized = (model or "").lower().split("/")[-1]
    patch_policy = _openai_patch_policy(normalized, detail)
    if patch_policy is not None:
        maximum, patch_budget, multiplier, reject_over_budget = patch_policy
        return ImageTokenEstimate(
            _openai_patch_tokens(
                width,
                height,
                maximum=maximum,
                patch_budget=patch_budget,
                multiplier=multiplier,
                reject_over_budget=reject_over_budget,
            ),
            "openai_patches",
            width,
            height,
            digest,
        )
    tile_policy: tuple[int, int] | None = None
    if normalized.startswith("gpt-4o-mini"):
        tile_policy = (2833, 5667)
    elif normalized.startswith(("gpt-4o", "gpt-4-turbo", "gpt-4-vision")):
        tile_policy = (85, 170)
    elif normalized.startswith("gpt-4.1") and not normalized.startswith(
        ("gpt-4.1-mini", "gpt-4.1-nano")
    ):
        tile_policy = (85, 170)
    elif normalized.startswith("gpt-5.1"):
        tile_policy = (70, 140)
    elif normalized.startswith(("o1", "o3")):
        tile_policy = (75, 150)
    if tile_policy is not None:
        tokens = _openai_tile_tokens(
            width,
            height,
            detail,
            base_tokens=tile_policy[0],
            tile_tokens=tile_policy[1],
        )
        return ImageTokenEstimate(
            tokens,
            "openai_tiles",
            width,
            height,
            digest,
        )
    if normalized.startswith(
        (
            "claude-3",
            "claude-sonnet-4",
            "claude-opus-4",
            "claude-haiku-4",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
        )
    ):
        if detail == "original":
            return ImageTokenEstimate(None, "unknown_image", width, height, digest)
        return ImageTokenEstimate(
            _anthropic_patch_tokens(width, height, normalized),
            "anthropic_patches",
            width,
            height,
            digest,
        )
    return ImageTokenEstimate(None, "unknown_image", width, height, digest)


@dataclass(frozen=True, slots=True)
class ImageReference:
    message_index: int
    block_index: int
    source_hash: str
    content_hash: str
    relative_path: str
    replacement: dict[str, str]


@dataclass(frozen=True, slots=True)
class ImageReferenceSelection:
    messages: list[dict[str, Any]]
    references: tuple[ImageReference, ...]


def _source_hash(block: dict[str, Any]) -> str:
    serialized = json.dumps(block, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def reference_verified_images(
    messages: list[dict[str, Any]],
    *,
    verified_message_indexes: set[int],
    workspace: Path | None,
) -> ImageReferenceSelection:
    """Reference only consumed images whose workspace file still matches exact sent bytes."""
    copied = deepcopy(messages)
    if workspace is None:
        return ImageReferenceSelection(copied, ())
    root = workspace.expanduser().resolve()
    references: list[ImageReference] = []
    for message_index in sorted(verified_message_indexes):
        if not 0 <= message_index < len(copied):
            continue
        content = copied[message_index].get("content")
        if not isinstance(content, list):
            continue
        for block_index, raw_block in enumerate(cast(list[object], content)):
            if not isinstance(raw_block, dict):
                continue
            block = cast(dict[str, Any], raw_block)
            if block.get("type") not in {"image_url", "input_image"}:
                continue
            payload = _image_payload(block)
            meta = block.get("_meta")
            path_value = cast(dict[str, Any], meta).get("path") if isinstance(meta, dict) else None
            if payload is None or not isinstance(path_value, str):
                continue
            candidate = Path(path_value).expanduser()
            candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            try:
                file_bytes = candidate.read_bytes()
            except OSError:
                continue
            sent_bytes, _detail = payload
            digest = hashlib.sha256(sent_bytes).hexdigest()
            if hashlib.sha256(file_bytes).hexdigest() != digest:
                continue
            relative_path = relative.as_posix()
            replacement = {
                "type": "text",
                "text": (
                    f"[Previously consumed image: {relative_path}; sha256={digest}. "
                    f"Use read_file with path '{relative_path}' to load it again.]"
                ),
            }
            content[block_index] = replacement
            references.append(
                ImageReference(
                    message_index,
                    block_index,
                    _source_hash(block),
                    digest,
                    relative_path,
                    replacement,
                )
            )
    return ImageReferenceSelection(copied, tuple(references))


def _image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for raw_block in cast(list[object], content):
            if isinstance(raw_block, dict) and cast(dict[str, Any], raw_block).get("type") in {
                "image_url",
                "image",
                "input_image",
            }:
                blocks.append(cast(dict[str, Any], raw_block))
    return blocks


def _decision_total(decisions: tuple[ContextDecision, ...]) -> int | None:
    final = {decision.source_id: decision.after_tokens for decision in decisions}
    if not all(value is not None for value in final.values()):
        return None
    return sum(cast(int, value) for value in final.values())


def reestimate_plan_images(
    plan: ContextPlan,
    messages: list[dict[str, Any]],
    model: str | None,
) -> ContextPlan:
    """Recalculate image source costs for this request's actual model."""
    from dataclasses import replace

    by_hash: dict[str, list[ImageTokenEstimate]] = {}
    for block in _image_blocks(messages):
        by_hash.setdefault(_source_hash(block), []).append(estimate_image_tokens(block, model))
    updates: dict[str, ImageTokenEstimate] = {}
    offsets: dict[str, int] = {}
    sources: list[ContextSource] = []
    for source in plan.sources:
        if source.kind != "image" or source.content_hash is None:
            sources.append(source)
            continue
        candidates = by_hash.get(source.content_hash, [])
        offset = offsets.get(source.content_hash, 0)
        estimate = candidates[offset] if offset < len(candidates) else ImageTokenEstimate(
            None, "unknown_image"
        )
        offsets[source.content_hash] = offset + 1
        updates[source.source_id] = estimate
        sources.append(
            replace(
                source,
                estimated_tokens=estimate.tokens,
                estimate_method=estimate.method,
            )
        )
    decisions = tuple(
        replace(
            decision,
            before_tokens=updates[decision.source_id].tokens,
            after_tokens=updates[decision.source_id].tokens,
        )
        if decision.source_id in updates and decision.action == "keep"
        else decision
        for decision in plan.decisions
    )
    return replace(
        plan,
        sources=tuple(sources),
        decisions=decisions,
        predicted_total=_decision_total(decisions),
    )


def apply_image_references(
    plan: ContextPlan,
    selection: ImageReferenceSelection,
) -> ContextPlan:
    """Append auditable transformations for verified image references."""
    from dataclasses import replace

    from nanobot.agent.context_plan import ContextDecision
    from nanobot.utils.helpers import estimate_prompt_tokens

    remaining = list(selection.references)
    appended: list[ContextDecision] = []
    for source in plan.sources:
        if source.kind != "image" or source.content_hash is None:
            continue
        match = next((item for item in remaining if item.source_hash == source.content_hash), None)
        if match is None:
            continue
        remaining.remove(match)
        after = estimate_prompt_tokens(
            [{"role": "user", "content": [match.replacement]}]
        ) - 4
        appended.append(
            ContextDecision(
                source.source_id,
                "reference",
                "selection",
                "image_reference",
                source.estimated_tokens,
                after,
            )
        )
    decisions = (*plan.decisions, *appended)
    return replace(plan, decisions=decisions, predicted_total=_decision_total(decisions))


def append_deferred_schema_audit(
    plan: ContextPlan,
    definitions: list[dict[str, Any]],
    deferred_names: tuple[str, ...],
) -> ContextPlan:
    """Account for intact schemas deliberately omitted by request-local discovery."""
    if not deferred_names:
        return plan
    from dataclasses import replace

    from nanobot.agent.context_plan import ContextDecision, source_for
    from nanobot.utils.helpers import estimate_prompt_tokens

    deferred = set(deferred_names)
    existing = {source.source_id for source in plan.sources}
    sources: list[ContextSource] = list(plan.sources)
    decisions: list[ContextDecision] = list(plan.decisions)
    for definition in definitions:
        name = schema_name(definition)
        source_id = f"schema:deferred:{name}"
        if name not in deferred or source_id in existing:
            continue
        source = source_for(
            source_id,
            "mcp_schema",
            definition,
            tokens=estimate_prompt_tokens([], [definition]),
        )
        sources.append(source)
        decisions.append(
            ContextDecision(
                source_id,
                "defer",
                "selection",
                "schema_deferred",
                source.estimated_tokens,
                0,
            )
        )
        existing.add(source_id)
    decision_tuple = tuple(decisions)
    return replace(
        plan,
        sources=tuple(sources),
        decisions=decision_tuple,
        predicted_total=_decision_total(decision_tuple),
    )


def estimate_model_request_tokens(
    provider: object | None,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[int | None, str]:
    """Count text/schema normally and inline images with known family formulas."""
    from nanobot.utils.helpers import estimate_prompt_tokens_chain

    image_tokens = 0
    text_messages = deepcopy(messages)
    found_image = False
    for message in text_messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text_blocks: list[object] = []
        for raw_block in cast(list[object], content):
            if isinstance(raw_block, dict) and cast(dict[str, Any], raw_block).get("type") in {
                "image_url",
                "image",
                "input_image",
            }:
                found_image = True
                estimate = estimate_image_tokens(cast(dict[str, Any], raw_block), model)
                if estimate.tokens is None:
                    return None, "unknown_image"
                image_tokens += estimate.tokens
            else:
                text_blocks.append(cast(object, raw_block))
        message["content"] = text_blocks
    text_tokens, source = estimate_prompt_tokens_chain(provider, model, text_messages, tools)
    return text_tokens + image_tokens, f"{source}+image_policy" if found_image else source
