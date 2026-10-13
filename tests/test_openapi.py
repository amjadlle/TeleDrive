"""OpenAPI contract and authorization regression tests."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import web.app as webapp


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(webapp, "WEB_USER", "apiadmin")
    monkeypatch.setattr(webapp, "WEB_PASSWORD", "long-test-password-12345")
    webapp.AUTH_FAILURES.clear()
    # No lifespan: documentation routes must not touch real system paths/Telegram.
    return TestClient(webapp.app, headers={"host": "localhost"})


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_requires_basic_auth(client, path):
    assert client.get(path).status_code == 401
    response = client.get(path, auth=("apiadmin", "long-test-password-12345"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store" or path in ("/docs", "/redoc")


def test_openapi_has_auth_and_typed_requests(client):
    response = client.get("/openapi.json", auth=("apiadmin", "long-test-password-12345"))
    schema = response.json()
    assert schema["info"]["title"] == "TeleDrive API"
    endpoints = schema["paths"]
    for path in ["/api/config", "/api/dashboard", "/api/files",
                 "/api/process/start", "/api/process/stop", "/api/auth",
                 "/api/health", "/api/logs"]:
        assert path in endpoints
    assert "/docs" not in endpoints
    assert schema["components"]["schemas"]["TelegramAuthRequest"]["properties"]["kind"]
    assert schema["components"]["schemas"]["StartRequest"]["properties"]["mode"]
    assert "HTTPBasic" in schema["components"]["securitySchemes"]


def test_swagger_and_redoc_render(client):
    swagger = client.get("/docs", auth=("apiadmin", "long-test-password-12345"))
    redoc = client.get("/redoc", auth=("apiadmin", "long-test-password-12345"))
    assert "swagger-ui" in swagger.text.lower()
    assert "redoc" in redoc.text.lower()
    assert "Content-Security-Policy" in swagger.headers


def test_api_mutations_need_csrf(client):
    response = client.post(
        "/api/process/start",
        auth=("apiadmin", "long-test-password-12345"),
        json={"mode": "run-once"},
    )
    assert response.status_code == 403


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"ok": True}
