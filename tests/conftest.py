import pytest
from validate.app import create_app
from validate.models import db as _db


@pytest.fixture()
def app():
    test_app = create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    with test_app.app_context():
        _db.create_all()
        yield test_app
        _db.drop_all()


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["reviewer_id"] = "tester"
        yield c


@pytest.fixture()
def db(app):
    from validate.models import db as _db
    return _db
