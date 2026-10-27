"""Database engine, sessions, and runtime settings lookup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, apply_overrides, base_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_engine(url: str | None = None) -> Engine:
    """(Re)create the engine. Called lazily, and by tests to point at a temp database."""
    global _engine, _session_factory
    if url is None:
        settings = base_settings()
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        url = settings.resolved_database_url
    if _engine is not None:
        _engine.dispose()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def create_all() -> None:
    from .models import Base

    Base.metadata.create_all(get_engine())


def new_session() -> Session:
    get_engine()
    assert _session_factory is not None
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def load_settings(session: Session) -> Settings:
    """Environment settings with Settings-page overrides from the database applied."""
    from .models import Setting

    rows = session.execute(select(Setting.key, Setting.value)).all()
    return apply_overrides(base_settings(), {k: v for k, v in rows})


def get_state(session: Session, key: str) -> str | None:
    """Internal key/value state (sync cursors, scheduler timestamps)."""
    from .models import Setting

    row = session.get(Setting, f"_state.{key}")
    return row.value if row else None


def set_state(session: Session, key: str, value: str) -> None:
    from .models import Setting

    row = session.get(Setting, f"_state.{key}")
    if row is None:
        session.add(Setting(key=f"_state.{key}", value=value))
    else:
        row.value = value
