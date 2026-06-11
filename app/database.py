import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import get_settings

Base = declarative_base()


def _make_engine():
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    kwargs = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=kwargs)


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
