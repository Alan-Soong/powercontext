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

"""Complete PDF page parsing, page-level Sources and evidence-page retrieval."""

from __future__ import annotations

import hashlib
import io
from functools import lru_cache
import math
import re
from collections import Counter
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel, field_validator, model_validator
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    SourceDefinition,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime.application import ScopedMemoryApplication

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"

_IDENTITY_PREFIX = "pdf"
_QUOTE_SAFE = ""
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TERM = re.compile(r"[a-z]+|\d+")
_BM25_K1 = 1.2
_BLEND_RELEVANCE = 1.0
_BLEND_COVERAGE = 0.75
_BLEND_HEADING = 0.5
_BLEND_SECTION = 1.5
_BLEND_DOCUMENT = 0.5
_POOL_LIMIT = 120
_MAX_COMPLEMENTARY_QUERIES = 16
_DOCUMENT_POOL = 20.0
_RRF_K = 60
_SECTION_WINDOW = 600
_SECTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "income": (
        re.compile(r"consolidated statements? of (income|operations|earnings)"),
        re.compile(r"statements? of operations"),
        re.compile(r"income statement"),
    ),
    "balance": (re.compile(r"consolidated balance sheets?"), re.compile(r"balance sheets?")),
    "cashflow": (
        re.compile(r"consolidated statements? of cash flows?"),
        re.compile(r"statements? of cash flows?"),
        re.compile(r"cash flow statement"),
    ),
    "equity": (
        re.compile(r"statements? of (stockholders|shareholders).? equity"),
        re.compile(r"comprehensive income"),
    ),
    "geographic": (re.compile(r"geographic"), re.compile(r"segment information"), re.compile(r"segments?")),
    "legal": (re.compile(r"legal proceedings"), re.compile(r"legal matters")),
    "risk": (re.compile(r"risk factors"),),
    "mda": (re.compile(r"management.s discussion"),),
    "tax": (re.compile(r"income taxes?"), re.compile(r"effective tax rate")),
    "debt": (re.compile(r"debt"), re.compile(r"borrowings"), re.compile(r"long-term obligations")),
    "segment": (re.compile(r"segment"), re.compile(r"business segment")),
    "customer": (re.compile(r"customer"), re.compile(r"concentration")),
    "summary": (re.compile(r"summary of financial performance"), re.compile(r"financial highlights")),
}
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "balance": (
        "balance",
        "asset",
        "liability",
        "liabilities",
        "liquidity",
        "quick ratio",
        "current ratio",
        "working capital",
        "inventory",
        "receivable",
        "goodwill",
        "equity",
        "cash equivalents",
        "debt",
        "borrowings",
        "leverage",
    ),
    "income": (
        "income",
        "margin",
        "revenue",
        "gross",
        "operating",
        "net",
        "ebitda",
        "earnings",
        "profit",
        "eps",
        "cost of sales",
        "expense",
        "tax",
    ),
    "cashflow": (
        "cash flow",
        "cash flows",
        "capex",
        "capital expenditure",
        "operating activities",
        "investing",
        "financing",
        "free cash",
        "depreciation",
        "amortization",
    ),
    "mda": ("drove", "driven", "change", "changes", "compared", "discussion", "results of operations"),
    "segment": ("segment", "segments", "business segment"),
    "geographic": ("geograph", "region", "regions", "international", "countries"),
    "legal": ("legal", "litigation", "proceedings", "lawsuit", "settlement"),
    "risk": ("risk", "risks"),
    "customer": ("customer", "customers", "client", "clients", "concentration"),
    "summary": ("summary", "performance"),
}
_BM25_B = 0.75
_HEADING_LINES = 3
_STOPWORDS = frozenset({
    "a", "about", "above", "after", "again", "all", "also", "am", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "did", "do",
    "does", "doing", "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he",
    "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not", "of", "off", "on",
    "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you",
    "your", "yours", "yourself", "yourselves",
})


class PDFDocumentInput(BaseModel):
    """One complete original PDF document and its caller-stable identity."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page and its extractable text."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_id(value)

    @field_validator("document_sha256")
    @classmethod
    def validate_document_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        return _require_page_number(value)


class PDFPageSource(Source):
    """A captured PDF page that keeps its document digest and physical page number."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        expected = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.source_id != expected:
            raise ValueError("PDF page source identity does not match its identity fields")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("PDF page Source name must equal its source identity")  # noqa: TRY003
        return self


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable identity for one physical page of one document revision."""

    document = _require_document_id(document_id)
    digest = _require_sha256(document_sha256)
    page = _require_page_number(page_number)
    identity = f"{_IDENTITY_PREFIX}:{quote(document, safe=_QUOTE_SAFE)}:{digest}:{page}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one original PDF in order."""

    document_id = _require_document_id(value.document_id)
    content = _require_bytes(value.content)
    digest = hashlib.sha256(content).hexdigest()
    reader = _open_reader(content)
    try:
        page_count = len(reader.pages)
    except Exception as error:  # noqa: BLE001 - any reader failure is an invalid archive
        raise ValueError("PDF content could not be read completely") from error
    if page_count < 1:
        raise ValueError("PDF content must contain at least one page")  # noqa: TRY003
    pages: list[PDFPageInput] = []
    for index in range(page_count):
        try:
            extracted = reader.pages[index].extract_text()
        except Exception as error:  # noqa: BLE001 - any page failure invalidates the document
            raise ValueError(f"PDF page {index + 1} could not be parsed") from error
        pages.append(
            PDFPageInput(
                document_id=document_id,
                document_sha256=digest,
                page_number=index + 1,
                text=(extracted or "").strip(),
            )
        )
    return tuple(pages)


class PDFPageSourceAdapter:
    """Resolve and read one PDF page Source."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as standard text evidence scoped to its document."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct evidence-page citations for one question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-blank string")  # noqa: TRY003
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")  # noqa: TRY003
    terms = _question_terms(question)
    if not terms:
        return ()
    pool = await _search_candidates(memory, question, terms)
    if not pool:
        return ()
    citations = [entry[0] for entry in pool]
    texts = [entry[1] for entry in pool]
    identities = [entry[2] for entry in pool]
    positions = list(range(len(pool)))
    intents = _question_intents(question)
    order = _rank_page_entries(terms, texts, positions, intents)
    order = _apply_document_routing(order, [text.split("\n", 1)[0] for text in texts], positions, terms)
    selected: list[MemoryCitation] = []
    seen: set[str] = set()
    for index in order:
        identity = identities[index]
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(citations[index])
        if len(selected) >= limit:
            break
    return tuple(selected)


async def _search_candidates(
    memory: ScopedMemoryApplication,
    question: str,
    terms: list[str],
) -> list[tuple[MemoryCitation, str, str]]:
    """Collect real Memory search hits from the question and its complementary queries."""

    try:
        from powercontext.builtin.runtime.models import SearchMemoryRequest
    except Exception:  # noqa: BLE001 - the Memory contract is optional for this helper
        return []
    from powercontext.builtin.artifacts.memory.models import MemoryCitation as _Citation

    pool: dict[str, tuple[MemoryCitation, str, str]] = {}
    for query in _candidate_queries(question, terms):
        try:
            page = await memory.search(SearchMemoryRequest(query=query, mode="auto", limit=_POOL_LIMIT))
        except Exception:  # noqa: BLE001 - a single unusable query must not stop the search
            continue
        for hit in page.hits:
            if hit.entry_id in pool:
                continue
            pool[hit.entry_id] = (
                _Citation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                ),
                hit.text,
                str(hit.entry_id),
            )
    return list(pool.values())


def _candidate_queries(question: str, terms: list[str]) -> tuple[str, ...]:
    """The original question plus complementary lexical and section queries."""

    queries: list[str] = [question]
    seen = {question.casefold()}
    intents = _question_intents(question)
    for intent in sorted(intents):
        phrase = _SECTION_QUERIES.get(intent)
        if phrase and phrase not in seen:
            seen.add(phrase)
            queries.append(phrase)
    ordered = list(terms)
    for start in range(0, min(len(ordered), 2 * (_MAX_COMPLEMENTARY_QUERIES - 1)), 2):
        pair = " ".join(ordered[start : start + 2])
        if pair and pair.casefold() not in seen:
            seen.add(pair.casefold())
            queries.append(pair)
    for term in ordered[:4]:
        if term.casefold() not in seen:
            seen.add(term.casefold())
            queries.append(term)
    return tuple(queries[:_MAX_COMPLEMENTARY_QUERIES])


_SECTION_QUERIES: dict[str, str] = {
    "balance": "consolidated balance sheets",
    "income": "consolidated statements of income",
    "cashflow": "consolidated statements of cash flows",
    "equity": "statements of stockholders equity",
    "mda": "management discussion and analysis",
    "segment": "segment information",
    "geographic": "geographic operations",
    "legal": "legal proceedings",
    "risk": "risk factors",
    "customer": "customer concentration",
    "summary": "summary of financial performance",
    "tax": "effective tax rate",
    "debt": "long-term debt",
}


async def _page_entries(memory: ScopedMemoryApplication) -> list[tuple[MemoryCitation, str, int, str]]:
    """Read every active page entry of the scoped Memory."""

    try:
        listing = await memory.list()
    except Exception:  # noqa: BLE001 - a Memory without listing still has to answer
        return []
    entries: list[tuple[MemoryCitation, str, int, str]] = []
    for record in listing.entries:
        if record.state != "active" or not record.entry.text.strip():
            continue
        source_ids = tuple(str(getattr(source, "source_id", "")) for source in record.entry.sources)
        key = source_ids[0] if source_ids else record.entry.entry_id
        entries.append((record.citation, record.entry.text, _page_number(record.entry.sources), key))
    return entries


async def _memory_search_order(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int,
) -> tuple[str, tuple[str, ...]]:
    """Ask the Memory for its own retrieval order and the mode that produced it."""

    try:
        from powercontext.builtin.runtime.models import SearchMemoryRequest
    except Exception:  # noqa: BLE001 - the runtime contract is optional for this helper
        return "", ()
    try:
        page = await memory.search(SearchMemoryRequest(query=question, mode="auto", limit=max(limit * 5, 100)))
    except Exception:  # noqa: BLE001 - no usable index means lexical ranking alone
        return "", ()
    return str(page.mode or ""), tuple(hit.entry_id for hit in page.hits)


def _named_documents(terms: list[str], documents: list[str]) -> frozenset[str]:
    """Match a question against the filing names it refers to."""

    if not terms:
        return frozenset()
    probe = set(terms) | {left + right for left, right in zip(terms, terms[1:])}
    named = set()
    for document in set(documents):
        tokens = {
            token
            for token in _TERM.findall(str(document).casefold())
            if len(token) >= 3 and not token.isdigit()
        }
        if tokens & probe:
            named.add(document)
    return frozenset(named)


def _apply_document_routing(
    ranked: list[int],
    documents: list[str],
    pages: list[int],
    terms: list[str] | None = None,
) -> list[int]:
    """Prefer filings named by the question, then concentrate on the best-scoring filing."""

    named = _named_documents(terms or (), documents)
    if named:
        return [index for index in ranked if documents[index] in named] + [
            index for index in ranked if documents[index] not in named
        ]
    scores = {index: 1.0 / (_RRF_K + rank) for rank, index in enumerate(ranked, start=1)}
    totals: dict[str, float] = {}
    for index, value in scores.items():
        document = documents[index]
        totals[document] = totals.get(document, 0.0) + value
    blended = {
        index: value + _BLEND_DOCUMENT * totals.get(documents[index], 0.0) / _DOCUMENT_POOL
        for index, value in scores.items()
    }
    return sorted(blended, key=lambda index: (-blended[index], pages[index], index))


def _fuse_with_memory_search(
    lexical: list[int],
    search_order: tuple[str, ...],
    entries: list[tuple[MemoryCitation, str, int, str]],
) -> list[int]:
    """Reciprocal-rank fusion of the lexical order and the Memory's own order."""

    if not search_order:
        return lexical
    position = {entry[0].entry_id: index for index, entry in enumerate(entries)}
    fused: dict[int, float] = {}
    for rank, index in enumerate(lexical, start=1):
        fused[index] = fused.get(index, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, entry_id in enumerate(search_order, start=1):
        index = position.get(entry_id)
        if index is not None:
            fused[index] = fused.get(index, 0.0) + 1.0 / (_RRF_K + rank)
    pages = [entry[2] for entry in entries]
    return sorted(fused, key=lambda index: (-fused[index], pages[index], index))


def _rank_page_entries(
    terms: list[str],
    texts: list[str],
    order: list[int],
    intents: frozenset[str],
) -> list[int]:
    """Blend lexical relevance, coverage, headings and statement structure."""

    tokenised = [_page_terms(text) for text in texts]
    lengths = [sum(counts.values()) or 1 for counts in tokenised]
    total = len(texts)
    average = (sum(lengths) / total) or 1.0
    document_frequency: Counter[str] = Counter()
    for counts in tokenised:
        document_frequency.update(counts.keys())
    inverse = {
        term: math.log(1 + (total - document_frequency.get(term, 0) + 0.5) / (document_frequency.get(term, 0) + 0.5))
        for term in terms
    }
    relevance: list[float] = []
    coverage: list[float] = []
    headings: list[float] = []
    sections: list[float] = []
    for index, counts in enumerate(tokenised):
        length = lengths[index]
        bm25 = 0.0
        rare = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            rare += inverse[term]
            bm25 += inverse[term] * (frequency * (_BM25_K1 + 1)) / (
                frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average)
            )
        relevance.append(bm25)
        coverage.append(rare)
        heading = _heading_terms(texts[index])
        headings.append(float(sum(1 for term in terms if term in heading)))
        sections.append(float(len(intents & _section_labels(texts[index]))) if intents else 0.0)
    peak_relevance = max(relevance) or 1.0
    peak_coverage = max(coverage) or 1.0
    denominator = float(max(1, len(intents)))
    blended = [
        _BLEND_RELEVANCE * relevance[index] / peak_relevance
        + _BLEND_COVERAGE * coverage[index] / peak_coverage
        + _BLEND_HEADING * headings[index] / _HEADING_LINES
        + _BLEND_SECTION * sections[index] / denominator
        for index in range(total)
    ]
    return sorted(range(total), key=lambda index: (-blended[index], order[index], index))


@lru_cache(maxsize=8192)
def _page_terms(text: str) -> Counter[str]:
    return Counter(_stem(term) for term in _TERM.findall(text.casefold()) if term not in _STOPWORDS)


@lru_cache(maxsize=8192)
def _heading_terms(text: str) -> frozenset[str]:
    lines = [line.strip() for line in text.split("\n") if line.strip()][: _HEADING_LINES + 1]
    if lines and " " not in lines[0] and lines[0].isascii():
        # The projection prefixes every page with its document id; headings start after it.
        lines = lines[1:]
    return frozenset(_stem(term) for term in _TERM.findall(" ".join(lines).casefold()))


@lru_cache(maxsize=8192)
def _section_labels(text: str) -> frozenset[str]:
    head = text[: _SECTION_WINDOW].casefold()
    return frozenset(
        name for name, patterns in _SECTION_PATTERNS.items() if any(pattern.search(head) for pattern in patterns)
    )


def _question_intents(question: str) -> frozenset[str]:
    text = question.casefold()
    return frozenset(
        name for name, keywords in _INTENT_KEYWORDS.items() if any(keyword in text for keyword in keywords)
    )


def _question_terms(question: str) -> list[str]:
    return list(
        dict.fromkeys(
            _stem(term) for term in _TERM.findall(question.casefold()) if term not in _STOPWORDS
        )
    )


def _stem(term: str) -> str:
    if len(term) > 5 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and term.endswith("es"):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def _page_number(sources: object) -> int:
    for source in sources:  # type: ignore[union-attr]
        tail = str(getattr(source, "source_id", "")).rsplit(":", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return 1 << 30


def _open_reader(content: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as error:  # noqa: BLE001 - any constructor failure is an invalid archive
        raise ValueError("PDF content must be a readable PDF document") from error
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception as error:  # noqa: BLE001 - unsupported encryption is an invalid input
            raise ValueError("PDF content requires a user password") from error
        if not decrypted:
            raise ValueError("PDF content requires a user password")  # noqa: TRY003
    return reader


def _require_document_id(document_id: object) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-blank string")  # noqa: TRY003
    return document_id


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_HEX.match(value):
        raise ValueError("document_sha256 must be a lowercase hexadecimal SHA-256")  # noqa: TRY003
    return value


def _require_page_number(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    return value


def _require_bytes(content: object) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, (bytearray, memoryview)):
        return bytes(content)
    raise TypeError("PDF content must be bytes")  # noqa: TRY003


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)

__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]


def _page_number(sources: object) -> int:
    for source in sources:  # type: ignore[union-attr]
        tail = str(getattr(source, "source_id", "")).rsplit(":", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return 1 << 30
