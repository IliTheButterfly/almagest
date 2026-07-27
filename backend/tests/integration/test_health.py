from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/system/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == __version__


def test_openapi_schema_is_generatable(client: TestClient) -> None:
    """The generated frontend/deviceagent clients are built from this document."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/system/health" in response.json()["paths"]


def test_operation_ids_are_bare_function_names(client: TestClient) -> None:
    """Keeps generated client methods called `health()`, not `health_api_system_health_get()`."""
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/system/health"]["get"]["operationId"] == "health"
