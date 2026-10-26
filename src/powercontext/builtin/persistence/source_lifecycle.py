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

"""Durable Source lifecycle dependency graph and per-scope availability state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import insert, select, update
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.errors import GenerationConflictError, InvalidStoredPayloadError
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.limits import MAX_SCOPE_ID_LENGTH

_JSON_VALUE = TypeAdapter(JsonValue)
LIFECYCLE_STATE_SCHEMA = "powercontext.source-lifecycle.v1"


@dataclass(frozen=True, slots=True)
class StoredSourceLifecycle:
    """One persisted lifecycle graph at a compare-and-swap generation."""

    state: dict[str, JsonValue]
    generation: int


class SourceLifecycleRepository:
    """Persist the lifecycle graph with generation compare-and-swap."""

    async def load(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
    ) -> StoredSourceLifecycle | None:
        _require_scope_id(scope_id)
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return StoredSourceLifecycle(state=_decode(row["state"]), generation=int(row["generation"]))

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        state: dict[str, JsonValue],
        /,
        *,
        expected_generation: int,
    ) -> int:
        _require_scope_id(scope_id)
        if type(expected_generation) is not int or expected_generation < 0:
            raise InvalidStoredPayloadError("source-lifecycle", scope_id, "generation must be a non-negative integer")
        payload = _encode(state)
        if expected_generation == 0:
            existing = await self.load(connection, scope_id)
            if existing is not None:
                raise GenerationConflictError(scope_id, None, existing.generation)
            await connection.execute(
                insert(SOURCE_LIFECYCLE_TABLE).values(
                    scope_id=scope_id,
                    state=payload,
                    generation=1,
                )
            )
            return 1
        result = await connection.execute(
            update(SOURCE_LIFECYCLE_TABLE)
            .where(
                SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_TABLE.c.generation == expected_generation,
            )
            .values(state=payload, generation=expected_generation + 1)
        )
        if result.rowcount != 1:
            raise GenerationConflictError(scope_id, expected_generation, None)
        return expected_generation + 1


def _require_scope_id(scope_id: str) -> None:
    if not isinstance(scope_id, str) or not scope_id or len(scope_id) > MAX_SCOPE_ID_LENGTH:
        raise InvalidStoredPayloadError("source-lifecycle", "scope", "scope id must be a bounded non-empty string")


def _encode(state: dict[str, JsonValue]) -> bytes:
    validated = _JSON_VALUE.validate_python(state)
    if not isinstance(validated, dict):
        raise InvalidStoredPayloadError("source-lifecycle", "state", "must be a JSON object")
    return json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(payload: Any) -> dict[str, JsonValue]:
    if not isinstance(payload, bytes):
        raise InvalidStoredPayloadError("source-lifecycle", "state", "payload column is not bytes")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidStoredPayloadError("source-lifecycle", "state", "payload is not UTF-8 JSON") from error
    validated = _JSON_VALUE.validate_python(decoded)
    if not isinstance(validated, dict):
        raise InvalidStoredPayloadError("source-lifecycle", "state", "must be a JSON object")
    return validated


__all__ = ["LIFECYCLE_STATE_SCHEMA", "SourceLifecycleRepository", "StoredSourceLifecycle"]
