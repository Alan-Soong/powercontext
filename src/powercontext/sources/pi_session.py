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

"""Pi session branch parsing, per-event Sources and resumable history capture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    SourceDefinition,
    TextEvidence,
)
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SOURCE_VERSION = "1"
PI_SESSION_CHECKPOINT_SCHEMA = "powercontext.pi-session.checkpoint.v1"
PI_SESSION_VERSION = 3

_IDENTITY_PREFIX = "pi"
_QUOTE_SAFE = ""
_JSON_SEPARATORS = (",", ":")
_CONTROL_EVENT_TYPES = frozenset({"session"})
_SUMMARY_EVENT_TYPES = frozenset({"compaction", "branch_summary"})


class PiSessionInput(BaseModel):
    """One complete Pi v3 session archive plus the branch leaf to select."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its complete input record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @field_validator("session_id", "event_id", "event_type", "role")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Pi event identity fields must be non-empty")  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def validate_content(self) -> PiEventInput:
        if not self.content:
            raise ValueError("Pi event content must not be empty")  # noqa: TRY003
        return self


class PiEventSource(Source):
    """A captured Pi session event that keeps its full raw record."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventSource:
        expected = pi_event_source_id(self.session_id, self.event_id)
        if self.source_id != expected:
            raise ValueError("Pi event source identity does not match its identity fields")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("Pi event Source name must equal its source identity")  # noqa: TRY003
        return self


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable, delimiter-safe identity for one Pi session event."""

    for field, value in (("session_id", session_id), ("event_id", event_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")  # noqa: TRY003
    identity = f"{_IDENTITY_PREFIX}:{quote(session_id, safe=_QUOTE_SAFE)}:{quote(event_id, safe=_QUOTE_SAFE)}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the selected branch of one Pi v3 session into visible events."""

    records = _read_records(value.content)
    if not records:
        raise ValueError("Pi session archive must start with a session header")  # noqa: TRY003
    session_id = _session_id(records[0])
    graph, order = _index_graph(records[1:])
    leaf = _select_leaf(order, value.leaf_id)
    events: list[PiEventInput] = []
    for record in _branch_records(graph, leaf):
        visible = _visible_event(record)
        if visible is None:
            continue
        role, body = visible
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=record.get("parentId"),
                event_type=record["type"],
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class PiEventSourceAdapter:
    """Resolve and read one Pi session event Source."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event as standard text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import only the visible events of the selected branch that are not yet stored."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SOURCE_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        events = parse_pi_session(PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id))
        imported = _decode_checkpoint(session.checkpoint)
        pending: list[PiEventInput] = []
        updated = dict(imported)
        for event in events:
            identity = pi_event_source_id(event.session_id, event.event_id)
            digest = _event_digest(event)
            previous = imported.get(identity)
            if previous is not None:
                if previous != digest:
                    raise ValueError(f"imported Pi event {event.event_id!r} was rewritten")  # noqa: TRY003
                continue
            pending.append(event)
            updated[identity] = digest
        for event in pending:
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={
                "schema": PI_SESSION_CHECKPOINT_SCHEMA,
                "events": dict(sorted(updated.items())),
            },
        )


def _read_records(content: object) -> list[dict[str, JsonValue]]:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("Pi session content must be bytes")  # noqa: TRY003
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be UTF-8 with an optional leading BOM") from error
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Pi session archive must contain one JSON record per line") from error
        if not isinstance(record, dict):
            raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003
        records.append(record)
    return records


def _session_id(header: dict[str, JsonValue]) -> str:
    version = header.get("version")
    if header.get("type") != "session" or type(version) is not int or version != PI_SESSION_VERSION:
        raise ValueError("Pi session archive must start with a version 3 session header")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header id must be a non-empty string")  # noqa: TRY003
    return session_id


def _index_graph(
    records: list[dict[str, JsonValue]],
) -> tuple[dict[str, dict[str, JsonValue]], list[str]]:
    graph: dict[str, dict[str, JsonValue]] = {}
    order: list[str] = []
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi session event id must be a non-empty string")  # noqa: TRY003
        if event_id in graph:
            raise ValueError(f"Pi session event id {event_id!r} is duplicated")  # noqa: TRY003
        parent = record.get("parentId")
        if parent is not None:
            if not isinstance(parent, str) or parent not in graph:
                raise ValueError(f"Pi session event {event_id!r} has a missing or cyclic parent")  # noqa: TRY003
        event_type = record.get("type")
        if not isinstance(event_type, str) or not event_type or event_type == "session":
            raise ValueError("Pi session records after the header need a non-session type")  # noqa: TRY003
        graph[event_id] = record
        order.append(event_id)
    return graph, order


def _select_leaf(order: list[str], leaf_id: str | None) -> str | None:
    if leaf_id is None:
        return order[-1] if order else None
    if not isinstance(leaf_id, str) or leaf_id not in order:
        raise ValueError(f"Pi session leaf {leaf_id!r} does not exist")  # noqa: TRY003
    return leaf_id


def _branch_records(
    graph: dict[str, dict[str, JsonValue]],
    leaf: str | None,
) -> list[dict[str, JsonValue]]:
    chain: list[dict[str, JsonValue]] = []
    current = leaf
    while current is not None:
        record = graph[current]
        chain.append(record)
        parent = record.get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()
    return chain


def _visible_event(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = record["type"]
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            body = _tool_result_body(message)
        elif role == "bashExecution":
            body = _bash_body(message)
        else:
            body = _message_body(message)
        return (role, body) if body else None
    if event_type in _SUMMARY_EVENT_TYPES:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return event_type, summary
    if event_type == "custom_message":
        body = _text_of(record.get("content"))
        return ("custom", body) if body else None
    return None


def _message_body(message: dict[str, JsonValue]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, JsonValue]) -> str:
    name = block.get("name")
    call_id = block.get("id")
    arguments = block.get("arguments")
    return f"[tool_call {name} {call_id}] {_compact_json(arguments)}"


def _tool_result_body(message: dict[str, JsonValue]) -> str:
    name = message.get("toolName")
    call_id = message.get("toolCallId")
    return f"[tool_result {name} {call_id}] {_text_of(message.get('content'))}"


def _bash_body(message: dict[str, JsonValue]) -> str:
    command = message.get("command")
    output = message.get("output")
    return f"$ {command if isinstance(command, str) else ''}\n{output if isinstance(output, str) else ''}"


def _text_of(content: JsonValue) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _compact_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS)


def _event_digest(event: PiEventInput) -> str:
    payload = {"session_id": event.session_id, "record": event.record}
    encoded = _compact_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_checkpoint(checkpoint: JsonValue) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") != PI_SESSION_CHECKPOINT_SCHEMA:
        raise ValueError("Pi session checkpoint has an unsupported shape")  # noqa: TRY003
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint must contain an events object")  # noqa: TRY003
    decoded: dict[str, str] = {}
    for event_id, digest in events.items():
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint entries must be strings")  # noqa: TRY003
        decoded[event_id] = digest
    return decoded


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)

__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CHECKPOINT_SCHEMA",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SOURCE_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
