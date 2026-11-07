"""Validated domain records; dictionaries are confined to serialization boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .config import DetectionConfig, positive_number

Decision = Literal[
    "Coordination supported", "Alternative explanation plausible", "Insufficient evidence"
]
Endorsement = Literal["endorses", "rebuts", "quotes", "unclear"]
Alignment = Literal["supported", "mixed", "unclear", "not_established"]


class Record(BaseModel):
    """Preserve extension fields and original field presence across wire round trips."""

    model_config = ConfigDict(frozen=True, extra="allow")

    def to_record(self) -> dict:
        return self.model_dump(mode="json", exclude_unset=True)


class Channel(Record):
    channel_key: StrictStr = Field(min_length=1)
    name: StrictStr = ""
    category: StrictStr = "unknown"
    rationale: StrictStr = ""


class Post(Record):
    post_key: StrictStr = Field(min_length=1)
    channel_key: StrictStr = Field(min_length=1)
    post_family_key: StrictStr = Field(min_length=1)
    timestamp_utc: StrictStr
    title: StrictStr = ""
    text_original: StrictStr = ""
    source_ref: StrictStr = ""

    @field_validator("timestamp_utc")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("A source timezone is required.")
        return value


class Message(Record):
    message_key: StrictStr = Field(min_length=1)
    post_key: StrictStr = Field(min_length=1)
    channel_key: StrictStr = Field(min_length=1)
    account_key: StrictStr | None = None
    timestamp_utc: StrictStr
    text_original: StrictStr = ""
    urls: tuple[StrictStr, ...] = ()
    source_ref: StrictStr = ""
    sender_type: StrictStr = "user"

    _aware_timestamp = field_validator("timestamp_utc")(Post.aware_timestamp.__func__)


class Case(Record):
    case_id: StrictStr = Field(min_length=1)
    name: StrictStr
    kind: Literal["candidate", "comparison"] = "comparison"
    account_keys: tuple[StrictStr, ...]
    peripheral_keys: tuple[StrictStr, ...] = ()
    pair_keys: tuple[StrictStr, ...] = ()
    post_keys: tuple[StrictStr, ...] = ()
    family_count: StrictInt = Field(default=0, ge=0)
    rank_score: float = 0
    description: StrictStr = ""
    review_status: StrictStr = "Not reviewed"
    account_count: StrictInt = Field(default=0, ge=0)


class PairEvent(Record):
    post_key: StrictStr
    family_key: StrictStr
    message_a: StrictStr
    message_b: StrictStr
    timestamp_a: StrictStr
    timestamp_b: StrictStr
    delta_seconds: float = Field(ge=0, allow_inf_nan=False)


class Pair(Record):
    pair_key: StrictStr
    account_a: StrictStr
    account_b: StrictStr
    observed: StrictInt = Field(ge=0)
    opportunities: StrictInt = Field(ge=0)
    null_mean: float = Field(ge=0, allow_inf_nan=False)
    null_p95: float = Field(ge=0, allow_inf_nan=False)
    null_variance: float = Field(ge=0, allow_inf_nan=False)
    score: float = Field(allow_inf_nan=False)
    retained: StrictBool
    sensitivity: dict[str, StrictInt]
    events: tuple[PairEvent, ...] = ()


class DatasetInfo(Record):
    dataset_id: StrictStr = Field(min_length=1)
    label: StrictStr
    synthetic: StrictBool = False
    manifest: dict[str, Any] = Field(default_factory=dict)

    @field_validator("manifest")
    @classmethod
    def validate_manifest(cls, value: dict) -> dict:
        positive_number(value.get("timestamp_precision_seconds", 1), "timestamp_precision_seconds")
        limitations = value.get("limitations", [])
        if not isinstance(limitations, list) or not all(
            isinstance(note, str) for note in limitations
        ):
            raise ValueError("limitations must be a list of strings")
        return value


class Dataset(DatasetInfo):
    channels: tuple[Channel, ...]
    posts: tuple[Post, ...]
    messages: tuple[Message, ...]
    comparisons: tuple[Case, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Dataset:
        channels = {channel.channel_key for channel in self.channels}
        posts = {post.post_key: post for post in self.posts}
        if len(channels) != len(self.channels) or len(posts) != len(self.posts):
            raise ValueError("Channel and post keys must be unique.")
        seen = set()
        if len({case.case_id for case in self.comparisons}) != len(self.comparisons):
            raise ValueError("Comparison case keys must be unique.")
        post_times = {
            post.post_key: datetime.fromisoformat(post.timestamp_utc.replace("Z", "+00:00"))
            for post in self.posts
        }
        for post in self.posts:
            if post.channel_key not in channels:
                raise ValueError(f"Unknown channel in post {post.post_key}")
        for message in self.messages:
            if message.message_key in seen:
                raise ValueError(f"Duplicate message key: {message.message_key}")
            seen.add(message.message_key)
            parent = posts.get(message.post_key)
            if parent is None or parent.channel_key != message.channel_key:
                raise ValueError(f"Unresolved or inconsistent parent for {message.message_key}")
            observed = datetime.fromisoformat(message.timestamp_utc.replace("Z", "+00:00"))
            if observed < post_times[parent.post_key]:
                raise ValueError(f"Comment predates its parent post: {message.message_key}")
        return self

    def info(self) -> DatasetInfo:
        return DatasetInfo.model_validate(
            self.model_dump(
                mode="json",
                exclude={"channels", "posts", "messages", "comparisons"},
                exclude_unset=True,
            )
        )


class RunInfo(Record):
    run_id: StrictStr = Field(min_length=1)
    dataset_id: StrictStr = Field(min_length=1)
    config: DetectionConfig
    diagnostics: dict[str, Any]
    synthetic: StrictBool = False

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, value):
        if isinstance(value, DetectionConfig):
            return value
        if not isinstance(value, dict):
            raise ValueError("Run config must be detector settings.")
        return DetectionConfig.from_mapping(value)


class AnalysisRun(RunInfo):
    pairs: tuple[Pair, ...]
    groups: tuple[Case, ...]

    def info(self) -> RunInfo:
        return RunInfo.model_validate(
            self.model_dump(mode="json", exclude={"pairs", "groups"}, exclude_unset=True)
        )


class Review(Record):
    run_id: StrictStr
    case_id: StrictStr
    reviewer: StrictStr = Field(min_length=1)
    decision: Decision
    reason: StrictStr = Field(min_length=10)
    claim: StrictStr = ""
    endorsement: Endorsement = "unclear"
    alignment: Alignment = "unclear"
    narrative_notes: StrictStr = ""
    evidence_refs: tuple[StrictStr, ...] = ()
    synthetic: StrictBool = False
    updated_at: StrictStr | None = None
    revision: StrictInt | None = None

    @model_validator(mode="after")
    def validate_annotation(self) -> Review:
        if not self.reviewer.strip() or len(self.reason.strip()) < 10:
            raise ValueError("A reviewer and at least ten characters of reasoning are required.")
        if (self.claim.strip() or self.alignment == "supported") and (
            not self.claim.strip() or not self.evidence_refs or not self.narrative_notes.strip()
        ):
            raise ValueError(
                "A narrative claim needs supporting comments and contextual reasoning."
            )
        return self


class Page[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[T, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    def metadata(self) -> dict:
        return {
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "returned": len(self.items),
            "has_more": self.has_more,
        }


class DatasetCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_count: int = Field(ge=0)
    post_count: int = Field(ge=0)
    message_count: int = Field(ge=0)
    account_count: int = Field(ge=0)


class AccountActivity(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_key: StrictStr
    comment_count: int = Field(ge=0)
    core: bool


class SharedLink(BaseModel):
    url: str
    account_keys: tuple[str, ...]
    accounts: int
    message_keys: tuple[str, ...]
    dataset_count: int
    message_count: int
    truncated: bool


class SharedText(BaseModel):
    text: str
    message_keys: tuple[str, ...]
    accounts: int
    message_count: int
    truncated: bool


class ContentMatches(BaseModel):
    links: tuple[SharedLink, ...] = ()
    texts: tuple[SharedText, ...] = ()


class CaseEvent(BaseModel):
    pair_key: str
    event: PairEvent


class DatasetView(DatasetInfo):
    channels: tuple[Channel, ...] = ()


class Concentration(BaseModel):
    group_comments: int = Field(ge=0)
    thread_comments: int = Field(ge=0)
    share: float = Field(ge=0, le=1)


class WindowSensitivity(BaseModel):
    window_seconds: int
    pairs_with_repeated_support: int = Field(ge=0)
    pair_count: int = Field(ge=0)


class StressAvailability(BaseModel):
    available: bool
    retained_pair_count: int = Field(ge=0)


class PageInfo(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int
    has_more: bool


class CaseDetail(Record):
    case: Case
    run: RunInfo
    dataset: DatasetView
    accounts: tuple[AccountActivity, ...]
    posts: tuple[Post, ...]
    messages: tuple[Message, ...]
    context_messages: tuple[Message, ...]
    pairs: tuple[Pair, ...]
    review: Review | None
    counterevidence: tuple[str, ...]
    content_matches: ContentMatches
    concentration: Concentration
    sensitivity: tuple[WindowSensitivity, ...]
    stress_test: StressAvailability
    pagination: dict[str, PageInfo] = Field(default_factory=dict)
    complete: bool = False
