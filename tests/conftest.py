import pytest

from growth.data import seed


@pytest.fixture(scope="session", autouse=True)
def seeded_db(tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("data") / "jet.duckdb"
    mp = pytest.MonkeyPatch()
    mp.setenv("JET_DB_PATH", str(path))
    seed.seed(path)
    yield path
    mp.undo()
