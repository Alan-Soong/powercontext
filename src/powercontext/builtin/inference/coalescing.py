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

"""Cross-call Embedding request coalescing with a bounded success-only LRU."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Batch:
    """One underlying provider request shared by every waiter for its texts."""

    usage: InferenceUsage = field(default_factory=lambda: InferenceUsage(requests=0))


class CoalescingEmbeddingModel:
    """Share in-flight text embeddings across callers and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int | float) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, asyncio.Future[tuple[EmbeddingVector, _Batch]]] = {}
        self._inflight: dict[str, asyncio.Future[tuple[EmbeddingVector, _Batch]]] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._batches: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per original input position, sharing unfinished work."""

        original = tuple(texts)
        if not original:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        self._ensure_loop()

        resolved: dict[str, EmbeddingVector] = {}
        waits: list[tuple[str, asyncio.Future[tuple[EmbeddingVector, _Batch]]]] = []
        for text in original:
            if text in resolved or any(text == waiting for waiting, _ in waits):
                continue
            cached = self._cached(text)
            if cached is not None:
                resolved[text] = cached
                continue
            waits.append((text, self._future_for(text)))

        outcomes = await asyncio.gather(
            *(asyncio.shield(future) for _, future in waits),
            return_exceptions=True,
        )
        batches: dict[int, _Batch] = {}
        for (text, _), outcome in zip(waits, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            vector, batch = outcome
            resolved[text] = vector
            batches[id(batch)] = batch
        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in original),
            usage=_merge_usage(tuple(batch.usage for batch in batches.values())),
        )

    def _ensure_loop(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(
                self._batch_loop(), name="embedding-coalescing-loop"
            )

    def _future_for(self, text: str) -> asyncio.Future[tuple[EmbeddingVector, _Batch]]:
        future = self._pending.get(text)
        if future is None:
            future = self._inflight.get(text)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(_consume_exception)
            self._pending[text] = future
            self._wake.set()
        return future

    async def _batch_loop(self) -> None:
        while True:
            await self._wake.wait()
            if self._coalesce_seconds > 0:
                await asyncio.sleep(self._coalesce_seconds)
            self._wake.clear()
            pending, self._pending = self._pending, {}
            texts = list(pending)
            for start in range(0, len(texts), self._max_batch_size):
                chunk = tuple(texts[start : start + self._max_batch_size])
                waiters = {text: pending[text] for text in chunk}
                for text, future in waiters.items():
                    self._inflight[text] = future
                task = asyncio.get_running_loop().create_task(self._run_batch(chunk, waiters))
                self._batches.add(task)
                task.add_done_callback(self._batches.discard)

    async def _run_batch(
        self,
        texts: tuple[str, ...],
        waiters: dict[str, asyncio.Future[tuple[EmbeddingVector, _Batch]]],
    ) -> None:
        batch = _Batch()
        try:
            result = await self._model.embed(texts)
            vectors = tuple(result.vectors)
            if len(vectors) != len(texts):
                raise ValueError(  # noqa: TRY003
                    f"embedding model returned {len(vectors)} vectors for {len(texts)} texts"
                )
            batch.usage = result.usage
        except BaseException as error:
            for text, future in waiters.items():
                self._inflight.pop(text, None)
                if not future.done():
                    future.set_exception(error)
            return
        for text, vector in zip(texts, vectors, strict=True):
            self._store(text, vector)
            future = waiters[text]
            self._inflight.pop(text, None)
            if not future.done():
                future.set_result((vector, batch))

    def _cached(self, text: str) -> EmbeddingVector | None:
        if self._cache_size <= 0:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _store(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _consume_exception(future: asyncio.Future[object]) -> None:
    """Mark an abandoned future's failure retrieved so it never logs a warning."""

    if not future.cancelled():
        future.exception()


def _merge_usage(usages: tuple[InferenceUsage, ...]) -> InferenceUsage:
    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    for usage in usages:
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = [
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescingEmbeddingModel",
]
