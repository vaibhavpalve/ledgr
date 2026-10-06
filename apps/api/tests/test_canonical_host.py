import pytest
from starlette.testclient import TestClient

from api.config import settings
from api.main import app


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "redirect_hosts", "boeklite.com, www.boeklite.com")
    monkeypatch.setattr(settings, "app_base_url", "https://boeklite.nl")
    return TestClient(app, follow_redirects=False)


def test_secondary_host_redirects_to_canonical_origin(configured: TestClient) -> None:
    response = configured.get("/login?next=%2Fbtw", headers={"host": "boeklite.com"})
    assert response.status_code == 308
    assert response.headers["location"] == "https://boeklite.nl/login?next=%2Fbtw"


def test_redirect_keeps_the_method(configured: TestClient) -> None:
    response = configured.post("/v1/anything", headers={"host": "www.boeklite.com:443"})
    assert response.status_code == 308
    assert response.headers["location"] == "https://boeklite.nl/v1/anything"


def test_health_is_never_redirected(configured: TestClient) -> None:
    response = configured.get("/health", headers={"host": "boeklite.com"})
    assert response.status_code == 200


def test_unlisted_host_is_not_redirected(configured: TestClient) -> None:
    response = configured.get("/health", headers={"host": "boeklite.nl"})
    assert response.status_code == 200


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redirect_hosts", "")
    client = TestClient(app, follow_redirects=False)
    assert client.get("/health", headers={"host": "boeklite.com"}).status_code == 200
