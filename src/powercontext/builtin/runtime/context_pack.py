# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Incremental Context Pack wire protocol with prefix reuse and full reconstruction."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from powercontext.errors import PowerContextError

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PREFIX = "sha256:"


class ContextPackError(PowerContextError, ValueError):
    """Raised when a Context Pack or delta violates the wire protocol."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete context body as ordered, reconstructing byte chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", _as_item_tuple(self.items))
        _require_digest(self.digest, "context pack digest")
        if self.digest != _digest_for(self.items):
            raise ContextPackError("context pack digest does not match its items")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ContextPackError("context pack byte_count must be a non-negative integer")
        if self.byte_count != len(self.to_bytes()):
            raise ContextPackError("context pack byte_count does not match its serialized size")

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build one complete pack from already chunked bytes."""

        prepared = _as_item_tuple(items)
        digest = _digest_for(prepared)
        return cls(items=prepared, digest=digest, byte_count=_fixed_point_byte_count(prepared, digest))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk one UTF-8 context body deterministically without changing its bytes."""

        if not isinstance(content, str):
            raise TypeError("context content must be a string")  # noqa: TRY003
        return cls.build(chunk_content(content, chunk_bytes=chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        """Decode and verify one complete pack payload."""

        payload = _load_object(data, "context pack")
        if set(payload) != _PACK_FIELDS:
            raise ContextPackError("context pack must contain exactly schema, items, digest and byte_count")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("context pack schema must be powercontext.context-pack.v1")
        items = _decode_items(payload["items"], "context pack items")
        digest = payload["digest"]
        byte_count = payload["byte_count"]
        _require_digest(digest, "context pack digest")
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("context pack byte_count must be a non-negative integer")
        pack = cls(items=items, digest=digest, byte_count=byte_count)
        if pack.byte_count != len(bytes(data)):
            raise ContextPackError("context pack byte_count does not match the payload length")
        return pack

    def to_bytes(self) -> bytes:
        """Serialize the complete pack as canonical JSON."""

        return _canonical_bytes({
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    def content(self) -> bytes:
        """Return the reconstructed body bytes."""

        return b"".join(self.items)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A retained base prefix plus the exact target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _require_digest(self.base_digest, "delta base_digest")
        _require_digest(self.target_digest, "delta target_digest")
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")
        object.__setattr__(self, "appended", _as_item_tuple(self.appended))

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Keep the longest shared chunk prefix and carry the target suffix."""

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise TypeError("between requires two ContextPack values")  # noqa: TRY003
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        """Decode and verify one delta payload."""

        payload = _load_object(data, "context pack delta")
        if set(payload) != _DELTA_FIELDS:
            raise ContextPackError(
                "context pack delta must contain exactly schema, base_digest, target_digest, prefix_items and appended"
            )
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("context pack delta schema must be powercontext.context-pack-delta.v1")
        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=payload["prefix_items"],
            appended=_decode_items(payload["appended"], "context pack delta appended"),
        )

    def to_bytes(self) -> bytes:
        """Serialize the delta as canonical JSON."""

        return _canonical_bytes({
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @property
    def byte_count(self) -> int:
        """Wire size of this delta, including every protocol field."""

        return len(self.to_bytes())


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Verify one delta against its base and rebuild the exact target pack."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise TypeError("apply_context_pack_delta requires a ContextPack and a ContextPackDelta")  # noqa: TRY003
    if base.digest != delta.base_digest:
        raise ContextPackError("delta was computed against a different base pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix_items exceeds the base chunk count")
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack does not match the delta target digest")
    return target


def chunk_content(content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> tuple[bytes, ...]:
    """Split one UTF-8 body into deterministic, bounded, valid-UTF-8 chunks."""

    if type(chunk_bytes) is not int or chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive integer")
    if not content:
        return ()
    encoded = content.encode("utf-8")
    chunks: list[bytes] = []
    for line in encoded.splitlines(keepends=True) or [encoded]:
        chunks.extend(_split_line(line, chunk_bytes))
    return tuple(chunks)


def pack_for_content(
    content: str,
    base: ContextPack | None = None,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> ContextPack:
    """Build a target pack that keeps as much of ``base``'s chunk prefix as it can."""

    if base is None:
        return ContextPack.from_content(content, chunk_bytes=chunk_bytes)
    raw = content.encode("utf-8")
    kept: list[bytes] = []
    offset = 0
    for item in base.items:
        if raw[offset : offset + len(item)] != item:
            break
        kept.append(item)
        offset += len(item)
    kept.extend(chunk_content(raw[offset:].decode("utf-8"), chunk_bytes=chunk_bytes))
    return ContextPack.build(tuple(kept))


def _split_line(line: bytes, chunk_bytes: int) -> list[bytes]:
    if len(line) <= chunk_bytes:
        return [line]
    pieces: list[bytes] = []
    current = bytearray()
    for char in line.decode("utf-8"):
        encoded = char.encode("utf-8")
        if len(encoded) > chunk_bytes:
            raise ContextPackError("chunk_bytes is too small to hold one UTF-8 character")
        if len(current) + len(encoded) > chunk_bytes:
            pieces.append(bytes(current))
            current = bytearray()
        current.extend(encoded)
    if current:
        pieces.append(bytes(current))
    return pieces


def _as_item_tuple(items: Iterable[bytes]) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, memoryview, str)):
        raise ContextPackError("context pack items must be an iterable of byte chunks")
    prepared: list[bytes] = []
    for item in items:
        if isinstance(item, bytes):
            prepared.append(item)
        elif isinstance(item, (bytearray, memoryview)):
            prepared.append(bytes(item))
        else:
            raise ContextPackError("context pack items must contain only bytes")
    return tuple(prepared)


def _digest_for(items: Sequence[bytes]) -> str:
    canonical = _canonical_bytes({"schema": PACK_SCHEMA, "items": _encode_items(items)})
    return _DIGEST_PREFIX + hashlib.sha256(canonical).hexdigest()


def _fixed_point_byte_count(items: Sequence[bytes], digest: str) -> int:
    byte_count = 0
    for _ in range(16):
        payload = _canonical_bytes({
            "schema": PACK_SCHEMA,
            "items": _encode_items(items),
            "digest": digest,
            "byte_count": byte_count,
        })
        if len(payload) == byte_count:
            return byte_count
        byte_count = len(payload)
    raise ContextPackError("context pack byte_count could not be resolved")


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Sequence[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object, label: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{label} must be a list of Base64 strings")  # noqa: TRY003
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError(f"{label} must contain only Base64 strings")  # noqa: TRY003
        try:
            raw = base64.b64decode(entry.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as error:
            raise ContextPackError(f"{label} contains invalid Base64") from error  # noqa: TRY003
        if base64.b64encode(raw).decode("ascii") != entry:
            raise ContextPackError(f"{label} contains non-canonical Base64")  # noqa: TRY003
        decoded.append(raw)
    return tuple(decoded)


def _load_object(data: bytes, label: str) -> dict[str, object]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContextPackError(f"{label} payload must be bytes")  # noqa: TRY003
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError(f"{label} payload must be UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label} payload must be a JSON object")  # noqa: TRY003
    return payload


def _require_digest(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError(f"{label} must use sha256:<lowercase hex>")  # noqa: TRY003
    hex_part = value[len(_DIGEST_PREFIX) :]
    if len(hex_part) != 64 or hex_part != hex_part.lower():
        raise ContextPackError(f"{label} must use sha256:<lowercase hex>")  # noqa: TRY003
    try:
        int(hex_part, 16)
    except ValueError as error:
        raise ContextPackError(f"{label} must use sha256:<lowercase hex>") from error  # noqa: TRY003


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
    "chunk_content",
    "pack_for_content",
]
