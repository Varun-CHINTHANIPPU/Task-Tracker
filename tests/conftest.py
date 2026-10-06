import pytest
from app.db import Database
from app.services import TrackerService


@pytest.fixture
def service(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    s = TrackerService(db)
    yield s
    db.close()
