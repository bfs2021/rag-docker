from __future__ import annotations
from typing import Any, Optional, Annotated, Literal
from pydantic import BaseModel, Field, BeforeValidator, ValidationError, field_validator, model_validator


def _numeric(value):
    if isinstance(value, bool):
        raise ValueError("A numeric setting cannot be a boolean")
    return value


IndexType = Literal["hnsw", "flat"]
DistanceMetric = Literal["cosine", "dot", "l2-squared"]
RetrievalMode = Literal["hnsw", "flat", "hybrid", "semantic"]
ResponseFormat = Literal["end_user", "engineer"]
ChunkingStrategy = Literal["fixed", "overlap", "language", "context_aware", "semantic"]
PositiveSize = Annotated[int, BeforeValidator(_numeric), Field(ge=1)]
NonnegativeSize = Annotated[int, BeforeValidator(_numeric), Field(ge=0)]
# Chunk bounds (#53): at least 50 characters, so a size-driven strategy can't
# be asked for one-character chunks; at most 6,000, about the 1,500 tokens the
# embedding model reads. `semantic` splits by similarity and ignores
# chunk_size, so these don't bound its chunks. They apply to settings being
# saved or used; a saved configuration from before them is still served and
# exported as it is.
ChunkSize = Annotated[int, BeforeValidator(_numeric), Field(ge=50, le=6000)]
MinChunkSize = Annotated[int, BeforeValidator(_numeric), Field(ge=0, le=6000)]
UnitInterval = Annotated[float, BeforeValidator(_numeric), Field(ge=0, le=1, allow_inf_nan=False)]
TopK = Annotated[int, BeforeValidator(_numeric), Field(ge=1, le=50)]
SEARCH_EF_MIN, SEARCH_EF_MAX = 16, 512
SearchEf = Annotated[int, BeforeValidator(_numeric), Field(ge=SEARCH_EF_MIN, le=SEARCH_EF_MAX)]
OVERLAP_RULE = "chunk_overlap must be smaller than chunk_size for overlap/language"


# ── Collections ──────────────────────────────────────────────────────────────

class HnswConfig(BaseModel):
    efConstruction: Annotated[int, BeforeValidator(_numeric), Field(ge=64, le=512)] = 128
    maxConnections: Annotated[int, BeforeValidator(_numeric), Field(ge=16, le=128)] = 64
    ef: SearchEf = 64


class CreateCollectionRequest(BaseModel):
    name: str
    index_type: IndexType = "hnsw"
    distance_metric: DistanceMetric = "cosine"
    hnsw_config: HnswConfig = HnswConfig()


class StoredHnswConfig(BaseModel):
    # Existing SDK/server settings may exceed the workbench's new-request UI
    # ranges. Preserve them during import/rebuild without accepting booleans,
    # zero/negative sizes or unsupported index/distance names.
    efConstruction: PositiveSize = 128
    maxConnections: PositiveSize = 64
    ef: Annotated[int, BeforeValidator(_numeric), Field(ge=-1)] = 64

    @field_validator("ef")
    @classmethod
    def _nonzero_ef(cls, value):
        if value == 0:
            raise ValueError("Stored ef must be -1 (dynamic) or positive")
        return value


class StoredCollectionRequest(CreateCollectionRequest):
    hnsw_config: StoredHnswConfig = StoredHnswConfig()


class CollectionInfo(BaseModel):
    name: str
    object_count: int
    index_type: str
    distance_metric: str
    created_at: Optional[str]
    hnsw_config: Optional[dict[str, int]] = None


class CollectionsResponse(BaseModel):
    collections: list[CollectionInfo]


# ── Ingest ────────────────────────────────────────────────────────────────────

class IngestUploadResponse(BaseModel):
    job_id: str
    status: str
    files_queued: int
    collection: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    files_total: int
    files_completed: int
    files_failed: int
    chunks_stored: int
    # Chunks sent to Weaviate and those it acknowledged without a per-chunk
    # error, including ones removed again because the check failed (#302).
    # Null on records written before they existed, where 0 would be untrue.
    chunks_submitted: int | None = None
    chunks_accepted: int | None = None
    errors: list[str]
    # Files the caller sent that could not be parsed at all; they never
    # became part of files_total, so without this they vanish silently. Also
    # inputs replaced by a later one with the same name in the upload (#22).
    skipped: list[str] = []
    # Earlier chunks of the same names that this job removed (#22). Records
    # written before the field existed read back as 0.
    chunks_replaced: int = 0


class IngestConfig(BaseModel):
    chunking_strategy: ChunkingStrategy = "overlap"
    chunk_size: ChunkSize = 1000
    chunk_overlap: NonnegativeSize = 200
    similarity_threshold: Optional[UnitInterval] = None
    min_chunk_size: MinChunkSize = 100

    @model_validator(mode="after")
    def _relationships(self):
        if self.chunking_strategy in ("overlap", "language") and self.chunk_overlap >= self.chunk_size:
            raise ValueError(OVERLAP_RULE)
        return self


class IngestConfigResponse(BaseModel):
    collection: str
    chunking_strategy: str
    chunk_size: int
    chunk_overlap: int
    similarity_threshold: Optional[float]
    min_chunk_size: int
    is_default: bool


# ── Retrieval config ──────────────────────────────────────────────────────────

# These bounds apply to saves. A saved or packaged ef that is an integer outside
# SEARCH_EF_MIN-SEARCH_EF_MAX was stored before PR #108 and is inactive, so
# export and import clear it to null (retrieval_config.normalize), not refuse it.
class SaveRetrievalConfigBody(BaseModel):
    collection: str
    retrieval_mode: RetrievalMode = "hnsw"
    top_k: TopK = 5
    alpha: UnitInterval = 0.75
    ef: Optional[SearchEf] = None
    response_format: ResponseFormat = "end_user"


class RetrievalConfigResponse(BaseModel):
    collection: str
    retrieval_mode: str
    top_k: int
    alpha: float
    ef: Optional[int]
    response_format: str
    is_default: bool


class HelpResponse(BaseModel):
    topic: str
    markdown: str


# ── Export / Import ───────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    collection: str
    include_models: bool = False


class ExportStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str


class ExportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    chunks_written: int
    filename: Optional[str]
    size_bytes: Optional[int]
    source_document_count: Optional[int]
    fidelity: Optional[str]
    # What the package actually carries. A request for models that could not be
    # honoured comes back false, with the reason in `warnings`.
    models_bundled: Optional[bool]
    retrieve_script: Optional[bool]
    warnings: list[str]
    error: Optional[str]

    error_code: Optional[str] = None
    error_detail: Optional[dict] = None

class ImportRequest(BaseModel):
    filename: str
    # Required, no default (spec §6.4): silently picking a conflict policy could
    # delete a collection the caller did not mean to touch.
    on_conflict: str

    @field_validator("on_conflict")
    @classmethod
    def _on_conflict(cls, v: str) -> str:
        allowed = ("abort", "rename", "replace")
        if v not in allowed:
            raise ValueError(f"on_conflict must be one of {allowed}, got {v!r}")
        return v


class ImportStartResponse(BaseModel):
    job_id: str
    status: str
    filename: str


class ImportedSessionMapping(BaseModel):
    source_session_id: str
    session_id: str
    collection: str


class ImportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    on_conflict: str
    # The name actually imported under; differs from original_collection when
    # on_conflict='rename' resolved a collision.
    collection: Optional[str]
    original_collection: Optional[str]
    chunks_written: int
    fidelity: Optional[str]
    renamed: bool
    notes: list[str]
    restored_sessions: list[ImportedSessionMapping] = Field(default_factory=list)
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class PackageSummary(BaseModel):
    filename: str
    size_bytes: int
    # Null when the archive could not be read; the listing still shows the file
    # so the user is not left wondering where it went.
    collection: Optional[str]
    chunk_count: Optional[int]
    fidelity: Optional[str]
    created_at: Optional[str]
    readable: bool


class PackageListResponse(BaseModel):
    packages: list[PackageSummary]


# ── Tuning ────────────────────────────────────────────────────────────────────

class _ChunkingFields(BaseModel):
    chunking_strategy: Optional[ChunkingStrategy] = None
    chunk_size: Optional[ChunkSize] = None
    chunk_overlap: Optional[NonnegativeSize] = None
    similarity_threshold: Optional[UnitInterval] = None
    min_chunk_size: Optional[MinChunkSize] = None

    @model_validator(mode="after")
    def _relationships(self):
        # Fields are already checked, so only the overlap rule can fail here.
        # A plain ValueError keeps the nested model's input out of the error.
        try:
            IngestConfig(**{name: getattr(self, name) for name in IngestConfig.model_fields
                            if getattr(self, name) is not None})
        except ValidationError:
            raise ValueError(OVERLAP_RULE) from None
        return self

    def has_chunking(self) -> bool:
        return any(getattr(self, f) is not None for f in
                   ("chunking_strategy", "chunk_size", "chunk_overlap",
                    "similarity_threshold", "min_chunk_size"))

    def chunking(self) -> dict:
        """Defaults match the ingest pipeline, so an omitted field means 'as before'."""
        return {
            "strategy": self.chunking_strategy or "overlap",
            "chunk_size": self.chunk_size if self.chunk_size is not None else 1000,
            "chunk_overlap": self.chunk_overlap if self.chunk_overlap is not None else 200,
            "similarity_threshold": (self.similarity_threshold
                                     if self.similarity_threshold is not None else 0.85),
            "min_chunk_size": self.min_chunk_size if self.min_chunk_size is not None else 100,
        }


class RechunkRequest(_ChunkingFields):
    collection: str


class ReembedRequest(_ChunkingFields):
    """Chunking fields are optional here, and only legal with `with-sources`."""
    collection: str


class ReindexRequest(BaseModel):
    collection: str
    index_type: Optional[IndexType] = None
    distance_metric: Optional[DistanceMetric] = None


class TuneStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str


class TuneJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str
    chunks_total: int
    chunks_written: int
    notes: list[str]
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class TuneOptionsResponse(BaseModel):
    collection: str
    fidelity: str
    source_document_count: int
    can_rechunk: bool
    can_reembed: bool
    can_reindex: bool
    note: str


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    collection: str
    retrieval_mode: RetrievalMode = "hnsw"
    top_k: TopK = 5
    alpha: UnitInterval = 0.75
    include_citations: bool = False
    response_format: ResponseFormat = "end_user"


class Citation(BaseModel):
    source_file: str
    chunk_index: int
    score: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    citations: Optional[list[Citation]]
    retrieval_latency_ms: int
    llm_latency_ms: int
    chunks_retrieved: int


# ── Gold Standard ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    collection: str
    sample_size: int = 20
    seed: Optional[int] = None

    @field_validator("sample_size", "seed", mode="before")
    @classmethod
    def _numeric(cls, value):
        if isinstance(value, bool):
            raise ValueError("Sampling settings cannot be booleans")
        return value

    @field_validator("sample_size")
    @classmethod
    def _sample_size(cls, value):
        if not 1 <= value <= 100:
            raise ValueError("sample_size must be between 1 and 100")
        return value


class GenerateResponse(BaseModel):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0


class GoldPair(BaseModel):
    pair_id: str
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str
    source_file: str
    chunk_index: int
    status: str


class SessionValidity(BaseModel):
    stale: bool = False
    stale_reason: Optional[str] = None
    stale_at: Optional[str] = None
    orphaned: bool = False
    orphaned_reason: Optional[str] = None
    orphaned_at: Optional[str] = None


class SessionImportProvenance(BaseModel):
    session_id: str
    collection: str
    imported_at: str


class SessionResponse(SessionValidity):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0
    pairs: list[GoldPair]
    collection: str
    errors: list[str] = []
    imported_from: SessionImportProvenance | None = None


class PatchPairRequest(BaseModel):
    status: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    ground_truth: Optional[str] = None

    @model_validator(mode="after")
    def _content_edit_needs_edited_status(self):
        """Rewriting a pair's content must be recorded as an edit.

        Without this a caller could change the question while the pair still
        reads "approved", and the export would ship content nobody approved
        under a status that says otherwise.
        """
        content = [f for f in ("question", "answer", "ground_truth")
                   if getattr(self, f) is not None]
        if content and self.status != "edited":
            raise ValueError(
                f"changing {', '.join(content)} requires status='edited'; "
                f"got status={self.status!r}")
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"approved", "edited", "rejected", "pending"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class RegenerateRequest(BaseModel):
    session_id: str
    pair_id: str


class SaveRequest(BaseModel):
    session_id: str
    filename: Optional[str] = None
    allow_historical: bool = False

    @field_validator("allow_historical", mode="before")
    @classmethod
    def _explicit_opt_in(cls, value):
        if not isinstance(value, bool):
            raise ValueError("allow_historical must be a boolean")
        return value


class SaveResponse(BaseModel):
    filename: str
    pairs_saved: int
    pairs_excluded: int
    download_url: str
    historical: bool = False
    session_validity: SessionValidity = SessionValidity()


# ── Metrics ───────────────────────────────────────────────────────────────────

class LatencyStats(BaseModel):
    p50: float
    p95: float
    p99: float
    mean: float
    count: int


class LatencyRecord(BaseModel):
    """One recorded query, for plotting latency over time."""
    timestamp: str
    collection: str
    retrieval_mode: str
    retrieval_ms: int
    llm_ms: int
    total_ms: int


class MetricsResponse(BaseModel):
    total_records: int
    retrieval_latency: LatencyStats
    llm_latency: LatencyStats
    total_latency: LatencyStats
    # Most recent records, oldest first so a chart reads left to right.
    # Bounded by the `limit` query parameter: the ring buffer holds up to 500
    # entries and the UI polls every 30s, so returning all of them every time
    # would be wasteful.
    history: list[LatencyRecord]


# ── Error ─────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
