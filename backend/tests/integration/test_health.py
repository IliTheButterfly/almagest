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


def test_no_two_modules_share_a_response_model_name(client: TestClient) -> None:
    """A model name reused in two route modules renames *both* in the document.

    FastAPI disambiguates a collision by fully qualifying every colliding name —
    `app__api__routes__stock__UndoResponse` — so defining `UndoResponse` in a new
    module silently renames the existing one, breaking client code for a route
    that was never touched. Two modules did exactly that (stock and
    provisioning); the second is now `ProvisioningUndo*`.

    Asserted on the whole document rather than per-model, because the failure is
    invisible where it is introduced.
    """
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    qualified = sorted(name for name in schemas if "__" in name)
    assert qualified == [], (
        "colliding schema names, so these are now qualified and any client alias "
        f"for the short name has broken: {qualified}"
    )
