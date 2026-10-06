import json
import shutil

import pytest

from growth.data import queries as q
from growth.ml import effects as fx
from runners.generate_data import build


@pytest.fixture(scope="session")
def built_db(tmp_path_factory):
    """Path to a freshly built database (market + true-model history + partners), once per session.
    The ground truth lands next to it as true_model.json."""
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


@pytest.fixture(scope="session")
def truth(built_db):
    """Ground truth. Only tests may read it; growth/ never does."""
    return json.loads((built_db.parent / "true_model.json").read_text())


@pytest.fixture(scope="session")
def effects(con):
    """Effects fitted once per session with fewer bootstrap draws than production, for speed."""
    return fx.fit(con, draws=30)
