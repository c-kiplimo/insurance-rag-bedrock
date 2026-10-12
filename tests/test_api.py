from fastapi.testclient import TestClient

from fakes import FakeCache, build_pipeline
from insurance_rag.api.main import create_app
from insurance_rag.bootstrap import App
from insurance_rag.config import Settings


class _Retriever:
    def __init__(self, up: bool) -> None:
        self.up = up

    def ping(self) -> bool:
        return self.up


def _client(opensearch_up: bool = True) -> TestClient:
    pipeline, _ = build_pipeline()
    rag = App(
        pipeline=pipeline, retriever=_Retriever(opensearch_up), cache=FakeCache(), os_client=None
    )  # type: ignore[arg-type]
    return TestClient(create_app(Settings(), rag=rag))


def test_query_ok_and_request_id_echoed():
    with _client() as c:
        r = c.post(
            "/v1/query",
            json={"question": "When is proof of loss due?"},
            headers={"x-request-id": "abc"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["citations"][0]["source_id"] == "S1"
    assert r.headers["x-request-id"] == "abc" and body["request_id"] == "abc"


def test_validation_rejects_short_question():
    with _client() as c:
        assert c.post("/v1/query", json={"question": "hi"}).status_code == 422


def test_healthz_does_not_touch_dependencies():
    with _client(opensearch_up=False) as c:
        assert c.get("/healthz").status_code == 200


def test_readyz_503_when_opensearch_down():
    with _client(opensearch_up=False) as c:
        assert c.get("/readyz").status_code == 503


def test_readyz_ok():
    with _client() as c:
        assert c.get("/readyz").json()["status"] == "ready"
