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

"""Source-dependency lifecycle: propagate withdrawal and restore through evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.errors import GenerationConflictError
from powercontext.builtin.persistence.source_lifecycle import (
    LIFECYCLE_STATE_SCHEMA,
    SourceLifecycleRepository,
    StoredSourceLifecycle,
)
from powercontext.sources.models import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime

_TOPIC_FAMILY = "topic-memory"
_KIND_SOURCE = "source"
_KIND_MEMORY = "memory"
_KIND_TOPIC = "topic"
_POLICIES = frozenset({"all", "any"})
_PURPOSES = frozenset({"generation", "audit"})


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration, event or reference violates its contract."""


class SourceLifecycleEvent(BaseModel):
    """One explicit availability transition for a registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(ge=1, strict=True)
    sequence: int = Field(ge=1, strict=True)
    available: bool = Field(strict=True)
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-blank string")  # noqa: TRY003
        return value


@dataclass(slots=True)
class _LifecycleNode:
    node_id: str
    kind: Literal["source", "memory", "topic"]
    ref: dict[str, JsonValue]
    parents: tuple[str, ...]
    policy: Literal["all", "any"]
    available: bool
    revision: int = 1
    sequence: int = 0
    reason: str | None = None

    def binding(self) -> tuple[Any, ...]:
        return (self.kind, self.ref, self.parents, self.policy, self.revision)

    def payload(self) -> dict[str, JsonValue]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "ref": dict(self.ref),
            "parents": list(self.parents),
            "policy": self.policy,
            "available": self.available,
            "revision": self.revision,
            "sequence": self.sequence,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> _LifecycleNode:
        kind = payload.get("kind")
        policy = payload.get("policy")
        parents = payload.get("parents")
        if kind not in {_KIND_SOURCE, _KIND_MEMORY, _KIND_TOPIC} or policy not in _POLICIES:
            raise SourceLifecycleError("source lifecycle state is corrupted")  # noqa: TRY003
        if not isinstance(parents, list) or not all(isinstance(parent, str) for parent in parents):
            raise SourceLifecycleError("source lifecycle state is corrupted")  # noqa: TRY003
        return cls(
            node_id=str(payload["node_id"]),
            kind=kind,  # type: ignore[arg-type]
            ref=_as_object(payload.get("ref")),
            parents=tuple(parents),
            policy=policy,  # type: ignore[arg-type]
            available=bool(payload.get("available")),
            revision=int(payload.get("revision") or 1),
            sequence=int(payload.get("sequence") or 0),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
        )


class LifecycleApplication:
    """Provide scoped Source lifecycle services for one Runtime."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> BuiltinRuntime:
        return self._runtime

    def for_scope(self, scope_id: str, /) -> SourceLifecycleService:
        return SourceLifecycleService(self, scope_id)

    async def inactive_topic_ids(self, scope_id: str, /) -> frozenset[str]:
        """Return Topic artifacts that currently have no support in one scope."""

        try:
            nodes = await self._load(scope_id)
        except SourceLifecycleError:
            return frozenset()
        inactive: set[str] = set()
        for node in nodes:
            if node.kind != _KIND_TOPIC or node.available:
                continue
            artifact_id = node.ref.get("artifact_id")
            if isinstance(artifact_id, str):
                inactive.add(artifact_id)
        return frozenset(inactive)

    async def _load(self, scope_id: str, /) -> list[_LifecycleNode]:
        stored = await self._read(scope_id)
        if stored is None:
            return []
        return _decode_nodes(stored.state)

    async def _read(self, scope_id: str, /) -> StoredSourceLifecycle | None:
        async with self._database().transaction() as connection:
            return await SourceLifecycleRepository().load(connection, scope_id)

    def _database(self) -> AsyncDatabase:
        provider = getattr(self._runtime, "_provider", None)
        database = getattr(provider, "database", None)
        if database is None:
            raise SourceLifecycleError("source lifecycle requires the built-in relational storage")  # noqa: TRY003
        return database


class SourceLifecycleService:
    """Register dependency nodes and propagate Source availability for one scope."""

    def __init__(self, application: LifecycleApplication, scope_id: str) -> None:
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise SourceLifecycleError("scope_id must be a non-empty string")  # noqa: TRY003
        self._application = application
        self._runtime = application.runtime
        self.scope_id = scope_id

    async def register_source(self, node_id: str, reference: SourceRef, /, *, revision: int = 1) -> None:
        _require_text("node_id", node_id)
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")  # noqa: TRY003
        if not isinstance(reference, SourceRef):
            raise SourceLifecycleError("source reference must be a SourceRef")  # noqa: TRY003
        await self._require_source(reference)
        await self._register(
            _LifecycleNode(
                node_id=node_id,
                kind=_KIND_SOURCE,
                ref=reference.model_dump(mode="json"),
                parents=(),
                policy="all",
                available=True,
                revision=revision,
            )
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        _require_text("node_id", node_id)
        _require_policy(policy)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory reference must be a MemoryCitation")  # noqa: TRY003
        await self._require_memory(citation)
        await self._register(
            _LifecycleNode(
                node_id=node_id,
                kind=_KIND_MEMORY,
                ref=citation.model_dump(mode="json"),
                parents=_require_parents(node_id, parents),
                policy=policy,  # type: ignore[arg-type]
                available=True,
            )
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        _require_text("node_id", node_id)
        _require_policy(policy)
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("topic reference must be an ArtifactRef")  # noqa: TRY003
        await self._require_topic(reference)
        await self._register(
            _LifecycleNode(
                node_id=node_id,
                kind=_KIND_TOPIC,
                ref=reference.model_dump(mode="json"),
                parents=_require_parents(node_id, parents),
                policy=policy,  # type: ignore[arg-type]
                available=True,
            )
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event, propagate it, and return every available node id."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")  # noqa: TRY003
        nodes, generation = await self._load_with_generation()
        index = {node.node_id: node for node in nodes}
        node = index.get(event.node_id)
        if node is None or node.kind != _KIND_SOURCE:
            raise SourceLifecycleError(f"node {event.node_id!r} is not a registered Source")  # noqa: TRY003
        if event.revision != node.revision:
            raise SourceLifecycleError(f"node {event.node_id!r} has lifecycle revision {node.revision}")  # noqa: TRY003
        if event.sequence < node.sequence:
            raise SourceLifecycleError(f"node {event.node_id!r} already accepted sequence {node.sequence}")  # noqa: TRY003
        if event.sequence == node.sequence:
            if node.available != event.available or node.reason != event.reason:
                raise SourceLifecycleError(f"node {event.node_id!r} rejected a conflicting replay")  # noqa: TRY003
            return _available_ids(nodes)

        previous = {item.node_id: item.available for item in nodes}
        node.available = event.available
        node.sequence = event.sequence
        node.reason = event.reason
        _propagate(nodes, index)
        changed = tuple(item for item in nodes if item.available != previous[item.node_id])
        await self._apply_native_state(changed)
        await self._save(nodes, expected_generation=generation)
        return _available_ids(nodes)

    async def resolve(self, node_id: str, /, *, purpose: str = "generation") -> Any:
        """Return the registered reference, enforcing generation eligibility."""

        _require_text("node_id", node_id)
        if purpose not in _PURPOSES:
            raise SourceLifecycleError(f"unknown resolve purpose {purpose!r}")  # noqa: TRY003
        nodes = await self._application._load(self.scope_id)
        index = {node.node_id: node for node in nodes}
        node = index.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"node {node_id!r} is not registered")  # noqa: TRY003
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node {node_id!r} is not available for generation")  # noqa: TRY003
        return _reference(node)

    async def _register(self, node: _LifecycleNode) -> None:
        nodes, generation = await self._load_with_generation()
        index = {item.node_id: item for item in nodes}
        existing = index.get(node.node_id)
        if existing is not None:
            if existing.binding() == node.binding():
                return
            raise SourceLifecycleError(f"node {node.node_id!r} is already registered differently")  # noqa: TRY003
        if node.kind != _KIND_SOURCE:
            for parent in node.parents:
                if parent not in index:
                    raise SourceLifecycleError(f"parent {parent!r} is not registered")  # noqa: TRY003
            node.available = _derive(node.policy, tuple(index[parent].available for parent in node.parents))
        elif node.available is not True:
            raise SourceLifecycleError("source nodes are registered as available")  # noqa: TRY003
        nodes.append(node)
        index[node.node_id] = node
        if node.kind == _KIND_MEMORY and not node.available:
            await self._runtime.memory.for_scope(self.scope_id).apply_lifecycle_state(
                _memory_citation(node),
                active=False,
                reason="registered without supporting evidence",
            )
        await self._save(nodes, expected_generation=generation)

    async def _apply_native_state(self, changed: Sequence[_LifecycleNode]) -> None:
        for node in changed:
            if node.kind != _KIND_MEMORY:
                continue
            await self._runtime.memory.for_scope(self.scope_id).apply_lifecycle_state(
                _memory_citation(node),
                active=node.available,
                reason=node.reason,
            )

    async def _load_with_generation(self) -> tuple[list[_LifecycleNode], int]:
        stored = await self._application._read(self.scope_id)
        if stored is None:
            return [], 0
        return _decode_nodes(stored.state), stored.generation

    async def _save(self, nodes: Sequence[_LifecycleNode], /, *, expected_generation: int) -> None:
        state = {
            "schema": LIFECYCLE_STATE_SCHEMA,
            "nodes": [node.payload() for node in nodes],
        }
        try:
            async with self._application._database().transaction() as connection:
                await SourceLifecycleRepository().save(
                    connection,
                    self.scope_id,
                    _as_object(state),
                    expected_generation=expected_generation,
                )
        except GenerationConflictError as error:
            raise SourceLifecycleError("source lifecycle state changed concurrently") from error

    async def _require_source(self, reference: SourceRef) -> None:
        try:
            async with self._runtime._context(self.scope_id) as context:
                entries = await context.sources.journal.entries()
        except Exception as error:  # noqa: BLE001 - any read failure means the reference is unusable
            raise SourceLifecycleError("registered Source reference is not readable") from error
        if not any(entry.source_ref == reference for entry in entries):
            raise SourceLifecycleError("registered Source reference is not readable")  # noqa: TRY003

    async def _require_memory(self, citation: MemoryCitation) -> None:
        from powercontext.builtin.runtime.models import GetMemoryEntryRequest

        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:  # noqa: BLE001 - any read failure means the citation is unusable
            raise SourceLifecycleError("registered Memory citation is not readable") from error

    async def _require_topic(self, reference: ArtifactRef) -> None:
        if reference.family != _TOPIC_FAMILY:
            raise SourceLifecycleError("registered Topic reference must be a topic-memory Artifact")  # noqa: TRY003
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                _TOPIC_FAMILY,
                reference.artifact_id,
                reference.revision,
            )
        except Exception as error:  # noqa: BLE001 - any read failure means the reference is unusable
            raise SourceLifecycleError("registered Topic Artifact is not readable") from error


def _decode_nodes(state: dict[str, JsonValue]) -> list[_LifecycleNode]:
    if state.get("schema") != LIFECYCLE_STATE_SCHEMA:
        raise SourceLifecycleError("source lifecycle state has an unsupported schema")  # noqa: TRY003
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        raise SourceLifecycleError("source lifecycle state is corrupted")  # noqa: TRY003
    return [_LifecycleNode.from_payload(_as_object(node)) for node in nodes]


def _propagate(nodes: Sequence[_LifecycleNode], index: dict[str, _LifecycleNode]) -> None:
    for node in nodes:
        if node.kind == _KIND_SOURCE:
            continue
        node.available = _derive(node.policy, tuple(index[parent].available for parent in node.parents))


def _derive(policy: str, support: tuple[bool, ...]) -> bool:
    return any(support) if policy == "any" else all(support)


def _available_ids(nodes: Sequence[_LifecycleNode]) -> list[str]:
    return sorted(node.node_id for node in nodes if node.available)


def _reference(node: _LifecycleNode) -> Any:
    if node.kind == _KIND_SOURCE:
        return SourceRef.model_validate(node.ref)
    if node.kind == _KIND_MEMORY:
        return MemoryCitation.model_validate(node.ref)
    return ArtifactRef.model_validate(node.ref)


def _memory_citation(node: _LifecycleNode) -> MemoryCitation:
    return MemoryCitation.model_validate(node.ref)


def _require_text(field: str, value: object) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise SourceLifecycleError(f"{field} must be a non-blank string")  # noqa: TRY003


def _require_policy(policy: object) -> None:
    if policy not in _POLICIES:
        raise SourceLifecycleError("policy must be 'all' or 'any'")  # noqa: TRY003


def _require_parents(node_id: str, parents: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parents, str) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents must be a sequence of node ids")  # noqa: TRY003
    resolved = tuple(parents)
    if not resolved:
        raise SourceLifecycleError("derived nodes require at least one parent")  # noqa: TRY003
    if len(set(resolved)) != len(resolved):
        raise SourceLifecycleError("parents must not repeat")  # noqa: TRY003
    for parent in resolved:
        _require_text("parent", parent)
        if parent == node_id:
            raise SourceLifecycleError("a node cannot depend on itself")  # noqa: TRY003
    return resolved


def _as_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise SourceLifecycleError("source lifecycle value must be a JSON object")  # noqa: TRY003
    return value


__all__ = [
    "LifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleService",
]
