"""OpenAPI contract tests independent of Starlette's optional HTTP test client."""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from starlette.requests import Request
import pytest

import web.app as webapp


def request():
    return Request({
        "type": "http", "method": "GET", "path": "/openapi.json",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8080),
        "scheme": "http", "query_string": b"",
    })


def test_api_schema_exposes_typed_requests_and_security():
    schema = webapp.app.openapi()
    assert schema["info"]["title"] == "TeleDrive API"
    paths = schema["paths"]
    for endpoint in ["/api/config", "/api/dashboard", "/api/files",
                     "/api/process/start", "/api/process/stop", "/api/auth",
                     "/api/health", "/api/logs"]:
        assert endpoint in paths
    assert "/docs" not in paths
    schemas = schema["components"]["schemas"]
    assert "mode" in schemas["StartRequest"]["properties"]
    assert "kind" in schemas["TelegramAuthRequest"]["properties"]
    assert "HTTPBasic" in schema["components"]["securitySchemes"]


def test_docs_routes_return_official_swagger_redoc_html():
    assert "Swagger" in webapp.swagger_docs(None).body.decode()
    assert "ReDoc" in webapp.redoc_docs(None).body.decode()
    assert "api/auth" in webapp.openapi_schema(None).body.decode()


def test_documentation_requires_http_basic(monkeypatch):
    monkeypatch.setattr(webapp, "WEB_USER", "apiadmin")
    monkeypatch.setattr(webapp, "WEB_PASSWORD", "strong-password-for-tests")
    webapp.AUTH_FAILURES.clear()
    with pytest.raises(HTTPException) as exc:
        webapp.require_auth(request(), None)
    assert exc.value.status_code == 401

    valid = HTTPBasicCredentials(username="apiadmin", password="strong-password-for-tests")
    assert webapp.require_auth(request(), valid) is None


def test_health_is_public():
    assert webapp.health() == {"ok": True}
