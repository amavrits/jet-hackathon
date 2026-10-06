import pytest

from growth.data import queries as q
from runners.generate_data import build


@pytest.fixture(scope="session")
def con(tmp_path_factory):
    """A freshly built database (market + true-model history + partners), once per test session."""
    tmp = tmp_path_factory.mktemp("db")
    build(tmp / "jet.duckdb", tmp / "true_model.json", verbose=False)
    c = q.connect(tmp / "jet.duckdb", read_only=True)
    yield c
    c.close()
