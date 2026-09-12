from api.main import app


def test_app_imports() -> None:
    assert app.title == "LEDGR API"
