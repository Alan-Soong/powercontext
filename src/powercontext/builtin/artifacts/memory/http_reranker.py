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

"""HTTP cross-encoder Memory reranking with bounded batches and global ordering."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"

_SCORE_FIELDS = frozenset({"rank", "score"})


class HTTPMemoryReranker:
    """Score Memory candidates through one HTTP rating service and order them globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every original rank once, then select ``limit`` ranks from the whole pool."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            expected_ranks = set(range(start + 1, start + 1 + len(batch)))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text}
                        for rank, candidate in zip(range(start + 1, start + 1 + len(batch)), batch, strict=True)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, expected_ranks))
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _ in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, expected_ranks: set[int]) -> dict[int, float]:
    """Validate one batch response and return its rank-to-score mapping."""

    payload = response.json()
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly one 'scores' field")  # noqa: TRY003
    entries = payload["scores"]
    if not isinstance(entries, list):
        raise ValueError("rerank response 'scores' must be a list")  # noqa: TRY003
    parsed: dict[int, float] = {}
    for entry in entries:
        rank, score = _parse_entry(entry)
        if rank not in expected_ranks:
            raise ValueError(f"rerank response rank {rank} is not part of this batch")  # noqa: TRY003
        if rank in parsed:
            raise ValueError(f"rerank response repeats rank {rank}")  # noqa: TRY003
        parsed[rank] = score
    if set(parsed) != expected_ranks:
        missing = sorted(expected_ranks - set(parsed))
        raise ValueError(f"rerank response is missing ranks {missing}")  # noqa: TRY003
    return parsed


def _parse_entry(entry: Any) -> tuple[int, float]:
    if not isinstance(entry, dict) or set(entry) != _SCORE_FIELDS:
        raise ValueError("each rerank score must contain exactly 'rank' and 'score'")  # noqa: TRY003
    rank = entry["rank"]
    if type(rank) is not int:
        raise ValueError("rerank score rank must be an integer")  # noqa: TRY003
    score = entry["score"]
    if isinstance(score, bool) or not isinstance(score, int | float):
        raise ValueError("rerank score must be a finite number")  # noqa: TRY003
    value = float(score)
    if not math.isfinite(value):
        raise ValueError("rerank score must be a finite number")  # noqa: TRY003
    return rank, value


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
