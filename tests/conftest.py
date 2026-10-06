import shutil

import pytest

from growth.data import queries as q
from runners.generate_data import build


@pytest.fixture(scope="session")
def built_db(tmp_path_factory):
    """Path to a freshly built database (market + true-model history + partners), once per session."""
    tmp = tmp_path_factory.mktemp("db")
    build(tmp / "jet.duckdb", tmp / "true_model.json", verbose=False)
    return tmp / "jet.duckdb"


@pytest.fixture(scope="session")
def con(built_db):
    """Read-only connection to the shared test database."""
    c = q.connect(built_db, read_only=True)
    yield c
    c.close()


@pytest.fixture
def writable_db(built_db, tmp_path):
    """A private copy of the test database for tests that write (listing changes, graph runs)."""
    path = tmp_path / "jet.duckdb"
    shutil.copy(built_db, path)
    return path
