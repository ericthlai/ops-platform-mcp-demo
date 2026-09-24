"""Engine and session plumbing. DATABASE_URL env var overrides the default SQLite file."""

import os
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

DEFAULT_DATABASE_URL = "sqlite:///ops_platform.db"

engine = create_engine(
    os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
