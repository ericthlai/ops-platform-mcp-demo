"""Shared fixtures: an isolated in-memory SQLite engine wired into the FastAPI app.

TestClient is used without a context manager on purpose: entering it would run the
app lifespan, which creates tables on the real file DB instead of the test engine.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from platform_api.db import get_session
from platform_api.main import app
from platform_api.seed import seed


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def seeded_engine(engine):
    seed(engine)
    return engine


@pytest.fixture
def session_override(seeded_engine):
    def override():
        with Session(seeded_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(session_override):
    return TestClient(app)
