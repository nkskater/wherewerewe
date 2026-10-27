from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from ..db import new_session


def get_session() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
