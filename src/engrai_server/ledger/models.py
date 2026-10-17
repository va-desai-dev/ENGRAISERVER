"""Ledger records: dated facts about the person the gateway serves.

An entry is either *pinned* (always sent, so it must stay short and stable) or
an *episode* (recalled by embedding similarity when a conversation touches it).
Dates are partial ISO strings because a life is remembered at mixed precision:
``2019``, ``2019-06`` and ``2019-06-14`` are all valid and all sortable.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PARTIAL_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
ROUTE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_ENTRIES = 5000


def parse_when(value: str) -> tuple[int, int, int]:
    """Return ``(year, month, day)`` with 0 for a part the date leaves out."""

    match = PARTIAL_DATE.fullmatch(value)
    if not match:
        raise ValueError("dates look like 2019, 2019-06 or 2019-06-14")
    year, month, day = (int(part) if part else 0 for part in match.groups())
    if month and not 1 <= month <= 12:
        raise ValueError("month must be from 01 to 12")
    if day:
        try:
            date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"{value} is not a calendar date") from exc
    return year, month, day


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    when: str | None = None
    until: str | None = None
    pinned: bool = False
    enabled: bool = True
    tags: list[str] = Field(default_factory=list, max_length=32)
    createdAt: str | None = None
    updatedAt: str | None = None

    @field_validator("title", "content")
    @classmethod
    def stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("when", "until", mode="before")
    @classmethod
    def blank_date_is_undated(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("when", "until")
    @classmethod
    def partial_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parse_when(value)
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return [tag.strip()[:60] for tag in value if tag.strip()]

    @model_validator(mode="after")
    def span_is_ordered(self) -> "LedgerEntry":
        if self.until and not self.when:
            raise ValueError("an end date needs a start date")
        if self.when and self.until and parse_when(self.until) < parse_when(self.when):
            raise ValueError("the end date is before the start date")
        return self

    @property
    def sort_key(self) -> tuple[int, int, int, int, str]:
        """Chronological order; undated entries follow every dated one."""

        if not self.when:
            return (1, 0, 0, 0, self.title.lower())
        return (0, *parse_when(self.when), self.title.lower())

    @property
    def date_label(self) -> str:
        if not self.when:
            return ""
        return f"{self.when} – {self.until}" if self.until else self.when

    @property
    def heading(self) -> str:
        return f"{self.title} ({self.date_label})" if self.when else self.title

    @property
    def embedded_text(self) -> str:
        """What the index embeds; changing this re-embeds the entry."""

        return f"{self.heading}\n{self.content}"

    def document(self, prefix: str) -> str:
        """The entry as the encoder should read it.

        Encoders trained on titled documents take the title inside the prefix,
        for example ``title: {title} | text: ``. A prefix without ``{title}``
        is simply put in front of the heading and text.
        """

        if "{title}" in prefix:
            return prefix.replace("{title}", self.heading) + self.content
        return prefix + self.embedded_text


class LedgerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How the model should refer to the ledger's subject; blank means "the user".
    subject_name: str = Field(default="", max_length=120)
    enabled_routes: list[str] = Field(default_factory=list, max_length=512)
    embedding_model_path: str = ""
    embedding_gpu_layers: int = Field(default=0, ge=0, le=999)
    # Some embedding models are trained with task prefixes, for example
    # "search_query: " and "search_document: ". Blank suits models without them.
    query_prefix: str = Field(default="", max_length=400)
    document_prefix: str = Field(default="", max_length=400)
    top_k: int = Field(default=8, ge=1, le=50)
    min_score: float = Field(default=0.35, ge=-1.0, le=1.0)
    recall_budget_tokens: int = Field(default=1200, ge=64, le=32768)
    recall_placement: Literal["system", "user"] = "system"

    @field_validator("subject_name")
    @classmethod
    def strip_subject(cls, value: str) -> str:
        return value.strip()

    @field_validator("enabled_routes")
    @classmethod
    def route_ids(cls, value: list[str]) -> list[str]:
        for route in value:
            if not ROUTE_ID.fullmatch(route):
                raise ValueError(f"{route!r} is not a route id")
        return sorted(set(value))

    @field_validator("embedding_model_path")
    @classmethod
    def absolute_gguf(cls, value: str) -> str:
        value = value.strip()
        if value and (not value.startswith("/") or not value.lower().endswith(".gguf")):
            raise ValueError("the embedding model must be an absolute .gguf path")
        return value
