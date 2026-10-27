"""SQLAlchemy models.

Queries stay plain (no SQLite-only features) so Postgres is a DATABASE_URL change.
Every generated row records `model` and `prompt_version` so it can be regenerated
selectively when either changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    plex_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    notify_target: Mapped[str | None] = mapped_column(String(1024))
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    gap_threshold_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    plex_rating_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    poster_url: Mapped[str | None] = mapped_column(String(1024))
    local_path: Mapped[str | None] = mapped_column(String(2048))  # set for folder imports
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    episodes: Mapped[list[Episode]] = relationship(back_populates="show")


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("show_id", "season", "episode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    episode: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(512))
    plex_rating_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    file_path: Mapped[str | None] = mapped_column(String(2048))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    air_date: Mapped[str | None] = mapped_column(String(32))
    plex_summary: Mapped[str | None] = mapped_column(Text)
    tmdb_overview: Mapped[str | None] = mapped_column(Text)
    # For multi-episode files (S01E01-E02): every episode after the first points at the first,
    # which holds the summary for the whole file.
    part_of_episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id"))

    show: Mapped[Show] = relationship(back_populates="episodes")

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    @property
    def order_key(self) -> tuple[int, int]:
        return (self.season, self.episode)

    @property
    def overview(self) -> str | None:
        return self.tmdb_overview or self.plex_summary


class EpisodeSource(Base):
    __tablename__ = "episode_sources"
    __table_args__ = (UniqueConstraint("episode_id", "source_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32))  # sidecar, embedded, whisper, overview
    text_path: Mapped[str | None] = mapped_column(String(2048))
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EpisodeSummary(Base):
    __tablename__ = "episode_summaries"
    __table_args__ = (UniqueConstraint("episode_id", "model", "prompt_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32))
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SeasonRollup(Base):
    __tablename__ = "season_rollups"
    __table_args__ = (UniqueConstraint("show_id", "season", "model", "prompt_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    season: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(64))
    episode_count: Mapped[int] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    # Internal matching keys only. What a viewer sees comes from character_states at or
    # before their boundary, because a later episode can reveal a different name.
    name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)


class CharacterState(Base):
    __tablename__ = "character_states"
    __table_args__ = (UniqueConstraint("character_id", "episode_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True
    )
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    name_as_seen: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    role: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    new_info: Mapped[str | None] = mapped_column(Text)
    relationships_json: Mapped[list[Any]] = mapped_column(JSON, default=list)


class WatchEvent(Base):
    __tablename__ = "watch_events"
    __table_args__ = (Index("ix_watch_events_user_episode", "user_id", "episode_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    watched_at: Mapped[datetime] = mapped_column(DateTime)
    percent_complete: Mapped[int] = mapped_column(Integer)
    tautulli_ref: Mapped[str | None] = mapped_column(String(64), unique=True)


class WatchProgress(Base):
    __tablename__ = "watch_progress"
    __table_args__ = (UniqueConstraint("user_id", "show_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    last_watched_episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id"))
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Recap(Base):
    __tablename__ = "recaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    boundary_episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"))
    length: Mapped[str] = mapped_column(String(16))  # quick, standard, deep
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued -> generating -> verifying -> done | failed
    draft_text: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual, webhook, cli
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "queue", "priority"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    queue: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued -> running -> done | failed
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), index=True)
    progress: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    timings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
