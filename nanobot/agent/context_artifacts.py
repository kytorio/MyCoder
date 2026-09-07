"""Durable, lossless tool artifacts rooted in an explicitly supplied runtime directory."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nanobot.utils.helpers import write_text_atomic

_ROOT_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    content_hash: str
    byte_length: int
    scope: str


@dataclass(frozen=True)
class ArtifactPage:
    text: str
    next_offset_chars: int | None
    content_hash: str


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8", errors="surrogatepass")).decode("ascii")


def _decode(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8", errors="surrogatepass")


def _safe_id(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PermissionError("Invalid artifact reference")


def _check_path(path: Path) -> None:
    """Reject links/reparse points before resolving; include every existing ancestor."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PermissionError("Artifact links are forbidden")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise PermissionError("Artifact path type forbidden")


class ToolResultArtifactStore:
    """Immutable records, checked on every access, including after restart.

    Capacity counts file bytes (encoded content, metadata and orphan evidence), not
    filesystem allocation units. New writes reserve their entire record before
    invoking the shared atomic writer. Same-root instances coordinate in-process;
    the injected runtime root must not have concurrent external writers.
    """

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Invalid artifact capacity")
        if ".." in root.parts:
            raise PermissionError("Artifact root traversal forbidden")
        absolute = root.absolute()
        _check_path(absolute)
        self.root = absolute.resolve()
        self.max_bytes = max_bytes
        with _LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(self.root, threading.RLock())
        with self._lock:
            _check_path(self.root)
            self.root.mkdir(parents=True, exist_ok=True)

    def put(self, session_key: str, call_id: str, content: str) -> ArtifactRef:
        if not session_key:
            raise PermissionError("Artifact scope required")
        content_hash = _digest(content)
        byte_length = len(content.encode("utf-8", errors="surrogatepass"))
        # Encoding bytes avoids JSON decoding normalizing literal UTF-16 surrogate pairs.
        record = json.dumps({
            "version": 1, "scope": _encode(session_key), "call_hash": _digest(call_id),
            "content_hash": content_hash, "byte_length": byte_length,
            "text": _encode(content), "encoding": "base64-utf8-surrogatepass",
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        ref = ArtifactRef(_digest(record), content_hash, byte_length, session_key)
        with self._lock:
            path = self.root / (ref.artifact_id + ".json")
            _check_path(path)
            if not path.exists():
                used = self._used_bytes(self.root)
                if used + len(record.encode("utf-8")) > self.max_bytes:
                    raise OSError("Artifact capacity exceeded")
                write_text_atomic(path, record, newline="")
            self.read(ref, session_key, self.max_bytes)
        return ref

    def _used_bytes(self, directory: Path) -> int:
        used = 0
        for path in directory.iterdir():
            _check_path(path)
            used += self._used_bytes(path) if path.is_dir() else path.stat().st_size
        return used

    def read(self, ref: ArtifactRef, scope: str, max_bytes: int) -> str:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("Invalid read limit")
        with self._lock:
            stored_ref, text = self._load(ref.artifact_id, scope)
        if ref != stored_ref:
            raise PermissionError("Artifact reference mismatch")
        if ref.byte_length > max_bytes:
            raise ValueError("Artifact exceeds read limit")
        return text

    def resolve(self, ref_id: str, scope: str) -> ArtifactRef:
        """Resolve an opaque ID only after verifying the record and session scope."""
        with self._lock:
            return self._load(ref_id, scope)[0]

    def _load(self, ref_id: str, scope: str) -> tuple[ArtifactRef, str]:
        _safe_id(ref_id)
        if not scope:
            raise PermissionError("Artifact scope required")
        path = self.root / (ref_id + ".json")
        _check_path(path)
        with path.open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValueError("Artifact exceeds store capacity")
        if hashlib.sha256(data).hexdigest() != ref_id:
            raise ValueError("Artifact checksum mismatch")
        raw: object = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("Invalid artifact record")
        record = cast(dict[str, object], raw)
        stored_scope, content_hash = record.get("scope"), record.get("content_hash")
        byte_length, text = record.get("byte_length"), record.get("text")
        if (not isinstance(stored_scope, str) or not isinstance(content_hash, str)
                or type(byte_length) is not int or not isinstance(text, str)):
            raise ValueError("Invalid artifact record")
        if record.get("encoding") != "base64-utf8-surrogatepass":
            raise ValueError("Invalid artifact encoding")
        stored_scope, text = _decode(stored_scope), _decode(text)
        if stored_scope != scope:
            raise PermissionError("Artifact scope mismatch")
        if (record.get("version") != 1 or _digest(text) != content_hash
                or len(text.encode("utf-8", errors="surrogatepass")) != byte_length):
            raise ValueError("Artifact checksum mismatch")
        return ArtifactRef(ref_id, content_hash, byte_length, stored_scope), text

    def read_page(
        self, ref: ArtifactRef, scope: str, *, offset_chars: int = 0, max_chars: int = 16384,
    ) -> ArtifactPage:
        if (type(offset_chars) is not int or offset_chars < 0
                or type(max_chars) is not int or max_chars <= 0):
            raise ValueError("Invalid page bounds")
        text = self.read(ref, scope, self.max_bytes)
        if offset_chars > len(text):
            raise ValueError("Offset exceeds artifact length")
        end = min(len(text), offset_chars + max_chars)
        return ArtifactPage(text[offset_chars:end], end if end < len(text) else None, ref.content_hash)
