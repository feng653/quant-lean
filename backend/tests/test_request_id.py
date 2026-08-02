from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.core.request_id import REQUEST_ID_HEADER, attach_request_id


def _client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(attach_request_id)

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/failure")
    async def failure() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "promotion_not_approved", "message": "blocked"},
        )

    return TestClient(app)


def test_request_id_is_server_generated_and_present_on_success() -> None:
    client = _client()
    response = client.get("/ok", headers={REQUEST_ID_HEADER: "caller-controlled"})

    assert response.status_code == 200
    request_id = response.headers[REQUEST_ID_HEADER.lower()]
    assert len(request_id) == 32
    assert request_id != "caller-controlled"


def test_request_id_is_available_for_structured_error_correlation() -> None:
    client = _client()
    response = client.get("/failure")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "promotion_not_approved"
    assert len(response.headers[REQUEST_ID_HEADER.lower()]) == 32
