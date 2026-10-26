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
_BLEND_HEADING = 0.75
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
    page = await memory.list()
    citations: list[MemoryCitation] = []
    texts: list[str] = []
    order: list[int] = []
    for record in page.entries:
        if record.state != "active" or not record.entry.text.strip():
            continue
        citations.append(record.citation)
        texts.append(record.entry.text)
        order.append(_page_number(record.entry.sources))
    if not citations:
        return ()
    ranked = _rank_page_entries(terms, texts, order)
    return tuple(citations[index] for index in ranked[:limit])


def _rank_page_entries(terms: list[str], texts: list[str], order: list[int]) -> list[int]:
    """Blend lexical relevance, rare-term coverage and heading matches into one ranking."""

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
    peak_relevance = max(relevance) or 1.0
    peak_coverage = max(coverage) or 1.0
    blended = [
        _BLEND_RELEVANCE * relevance[index] / peak_relevance
        + _BLEND_COVERAGE * coverage[index] / peak_coverage
        + _BLEND_HEADING * headings[index] / _HEADING_LINES
        for index in range(total)
    ]
    return sorted(range(total), key=lambda index: (-blended[index], order[index], index))


def _page_terms(text: str) -> Counter[str]:
    return Counter(term for term in _TERM.findall(text.casefold()) if term not in _STOPWORDS)


def _heading_terms(text: str) -> frozenset[str]:
    lines = [line.strip() for line in text.split("\n") if line.strip()][: _HEADING_LINES + 1]
    # The projection prefixes every page with its document id; headings start after it.
    heading = " ".join(lines[1:])
    return frozenset(_TERM.findall(heading.casefold()))


def _question_terms(question: str) -> list[str]:
    return [
        term
        for term in dict.fromkeys(_TERM.findall(question.casefold()))
        if term not in _STOPWORDS
    ]


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
