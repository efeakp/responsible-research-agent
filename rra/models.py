"""Data models. Every verdict carries its reasons so the report never asserts
more than the evidence supports."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ExistenceVerdict(str, Enum):
    VERIFIED = "verified"                # identifier resolves and metadata matches
    METADATA_MISMATCH = "metadata_mismatch"  # resolves, but title/author/year disagree
    FOUND_BY_SEARCH = "found_by_search"  # no identifier given, strong title match found
    NOT_FOUND = "not_found"              # identifier or link doesn't exist, or article-shaped and nowhere
    EXISTS_UNINDEXED = "exists_unindexed"  # real (registered DOI / live link) but not in scholarly databases
    UNVERIFIABLE = "unverifiable"        # no identifier or link, not article-shaped, not found: a person checks
    RETRACTED = "retracted"              # exists but has been retracted
    UNCHECKED = "unchecked"              # lookup failed (network error etc.)


class SupportVerdict(str, Enum):
    SUPPORTED = "supported"                  # model found a literal evidence quote
    PARTIAL = "partially_supported"
    NOT_SUPPORTED = "not_supported"          # source text contradicts or doesn't cover it
    INSUFFICIENT = "insufficient_evidence"   # no abstract/text available to judge
    NEEDS_REVIEW = "needs_human_review"      # heuristic only, or model output failed checks


class Reference(BaseModel):
    key: str                      # how the manuscript cites it, e.g. "1"
    raw: str                      # the reference-list line as written
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pmid: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    first_author: Optional[str] = None
    year: Optional[int] = None


class SourceRecord(BaseModel):
    """What an authoritative database says about a work."""
    source: str                   # crossref | openalex | arxiv | pubmed
    identifier: str
    title: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    abstract: Optional[str] = None
    is_retracted: bool = False
    url: Optional[str] = None


class ExistenceResult(BaseModel):
    reference: Reference
    verdict: ExistenceVerdict
    record: Optional[SourceRecord] = None
    title_similarity: Optional[float] = None
    issues: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    id: str
    text: str                     # the sentence as written
    cited_keys: list[str]


class SupportResult(BaseModel):
    claim_id: str
    reference_key: str
    verdict: SupportVerdict
    method: str                   # "llm:<model>" or "heuristic"
    evidence_quote: Optional[str] = None
    rationale: str = ""
    evidence_scope: str = "abstract"   # what text was judged against


class RunReport(BaseModel):
    run_id: str
    manuscript: str
    manuscript_sha256: str
    started_at: str
    finished_at: Optional[str] = None
    tool_version: str
    references: list[ExistenceResult] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    support: list[SupportResult] = Field(default_factory=list)
    unresolved_citations: list[str] = Field(default_factory=list)  # cited keys with no reference entry
    uncited_references: list[str] = Field(default_factory=list)
