"""Text embedding for the vector channel.

Two providers are supported:

``hash``   (default) A deterministic, dependency-free embedder: character
           n-grams and words are hashed into a fixed-width unit vector with
           signed random projections. It needs no model download and no
           network access, so the whole project stays reproducible offline.

``openai`` Any OpenAI-compatible ``/embeddings`` endpoint (bge, gte, qwen,
           text-embedding-3-*, ...). Configure ``WP_EMBEDDING_*`` and the
           provider switches without touching the call sites.

Both providers return L2-normalised vectors, so ``cosine_distance`` in seekdb
is a well-behaved ranking signal.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

import httpx

from .config import settings

_ASCII_WORD = re.compile(r"[a-z0-9_+#.]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch if (ch.isalnum() or _CJK.match(ch) or ch in " \n\t") else " " for ch in text)


def tokens(text: str) -> list[str]:
    """Tokenise mixed Chinese/English text without external dictionaries."""
    norm = _normalize(text)
    out: list[str] = []
    for word in _ASCII_WORD.findall(norm):
        if len(word) > 1:
            out.append("w:" + word)
            if len(word) > 4 and word.endswith("s"):
                out.append("w:" + word[:-1])
    cjk_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", norm)
    for run in cjk_runs:
        for i, ch in enumerate(run):
            out.append("c:" + ch)
            if i + 1 < len(run):
                out.append("b:" + run[i : i + 2])
                if i + 2 < len(run):
                    out.append("t:" + run[i : i + 3])
    return out


def _slot(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % settings.embedding_dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


def hash_embed(text: str) -> list[float]:
    dim = settings.embedding_dim
    vec = [0.0] * dim
    toks = tokens(text)
    if not toks:
        return vec
    counts: dict[str, int] = {}
    for tok in toks:
        counts[tok] = counts.get(tok, 0) + 1
    for tok, count in counts.items():
        index, sign = _slot(tok)
        vec[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [round(v / norm, 6) for v in vec]


def _api_embed(texts: list[str]) -> list[list[float]]:
    if not settings.embedding_base_url:
        raise RuntimeError("WP_EMBEDDING_BASE_URL is required when WP_EMBEDDING_PROVIDER=openai")
    url = settings.embedding_base_url.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
    payload = {
        "model": settings.embedding_model,
        "input": texts,
        "dimensions": settings.embedding_dim,
    }
    with httpx.Client(timeout=60) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    vectors = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]
    out: list[list[float]] = []
    dim = settings.embedding_dim
    for vec in vectors:
        vec = [float(x) for x in vec[:dim]]
        if len(vec) < dim:
            vec += [0.0] * (dim - len(vec))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([round(v / norm, 6) for v in vec])
    return out


def embed(text: str) -> list[float]:
    if settings.embedding_provider == "openai":
        return _api_embed([text])[0]
    return hash_embed(text)


def embed_many(texts: list[str]) -> list[list[float]]:
    if settings.embedding_provider == "openai":
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            out.extend(_api_embed(texts[start : start + 32]))
        return out
    return [hash_embed(t) for t in texts]


def vector_literal(vec: list[float]) -> str:
    """seekdb accepts vectors as JSON-array strings."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def provider_label() -> str:
    if settings.embedding_provider == "openai":
        return f"openai-compatible:{settings.embedding_model}"
    return f"local-hash-ngram:{settings.embedding_dim}d"
