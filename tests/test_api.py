from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cache_service.api import create_app
from cache_service.models import Payload, Transformation
from cache_service.settings import Settings


def test_sample_payload_and_cache(tmp_path: Path) -> None:
    transformer = Mock(side_effect=str.upper)
    app = create_app(Settings(data_dir=tmp_path), transformer)
    body = {
        "list_1": ["first string", "second string", "third string"],
        "list_2": ["other string", "another string", "last string"],
    }
    with TestClient(app) as client:
        created = client.post("/payload", json=body)
        assert created.status_code == 201
        payload_id = created.json()["id"]
        assert created.json()["reused"] is False
        assert created.headers["Location"] == f"/payload/{payload_id}"
        assert client.get(f"/payload/{payload_id}").json() == {
            "output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, "
            "THIRD STRING, LAST STRING"
        }
        repeated = client.post("/payload", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["id"] == payload_id
        assert repeated.json()["reused"] is True
        assert transformer.call_count == 6
    assert (tmp_path / "payloads" / f"{payload_id}.json").is_file()


@pytest.mark.parametrize(
    "body",
    [
        {"list_1": ["one"], "list_2": []},
        {"list_1": [1], "list_2": ["one"]},
        {"list_1": [None], "list_2": ["one"]},
        {"list_1": "one", "list_2": ["one"]},
        {"list_1": ["one"]},
        {"list_1": [], "list_2": [], "extra": "unexpected"},
        {"list_1": ["x" * 10_001], "list_2": ["one"]},
        {"list_1": ["x"] * 1_001, "list_2": ["x"] * 1_001},
    ],
)
def test_invalid_requests_do_not_call_transformer(tmp_path: Path, body: dict) -> None:
    transformer = Mock(side_effect=str.upper)
    with TestClient(create_app(Settings(data_dir=tmp_path), transformer)) as client:
        assert client.post("/payload", json=body).status_code == 422
    transformer.assert_not_called()


def test_missing_payload_and_invalid_identifier(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get(f"/payload/{'a' * 64}").status_code == 404
        assert client.get("/payload/not-a-valid-id").status_code == 422
        assert client.get("/health").json() == {"status": "ok"}


def test_duplicates_and_partial_cache_hits(tmp_path: Path) -> None:
    transformer = Mock(side_effect=str.upper)
    with TestClient(create_app(Settings(data_dir=tmp_path), transformer)) as client:
        first = client.post("/payload", json={"list_1": ["one", "one"], "list_2": ["two", "one"]})
        assert transformer.call_count == 2
        assert client.get(first.headers["Location"]).json()["output"] == "ONE, TWO, ONE, ONE"
        second = client.post("/payload", json={"list_1": ["two"], "list_2": ["new"]})
        assert second.status_code == 201
        assert transformer.call_count == 3
        assert client.get(second.headers["Location"]).json()["output"] == "TWO, NEW"


def test_identical_outputs_reuse_identifier(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        first = client.post("/payload", json={"list_1": ["hello"], "list_2": ["world"]})
        second = client.post("/payload", json={"list_1": ["HELLO"], "list_2": ["WORLD"]})
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"list_1": [], "list_2": []}, ""),
        ({"list_1": [""], "list_2": [""]}, ", "),
        ({"list_1": ["привет", "straße"], "list_2": ["мир", "😊"]}, "ПРИВЕТ, МИР, STRASSE, 😊"),
        ({"list_1": ["a, b"], "list_2": ["c\nd"]}, "A, B, C\nD"),
    ],
)
def test_edge_case_payloads(tmp_path: Path, body: dict, expected: str) -> None:
    transformer = Mock(side_effect=str.upper)
    with TestClient(create_app(Settings(data_dir=tmp_path), transformer)) as client:
        created = client.post("/payload", json=body)
        assert created.status_code == 201
        assert client.get(created.headers["Location"]).json() == {"output": expected}
        repeated = client.post("/payload", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["id"] == created.json()["id"]
        assert transformer.call_count == len(set(body["list_1"] + body["list_2"]))


def test_cache_and_files_survive_application_restart(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    body = {"list_1": ["hello"], "list_2": ["world"]}
    with TestClient(create_app(settings)) as client:
        created = client.post("/payload", json=body)
    transformer = Mock(side_effect=AssertionError("Must use the persisted cache"))
    with TestClient(create_app(settings, transformer)) as restarted:
        assert restarted.get(created.headers["Location"]).json() == {"output": "HELLO, WORLD"}
        repeated = restarted.post("/payload", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["id"] == created.json()["id"]
    transformer.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("external failure"), 42])
def test_transformer_failure_rolls_back_request(tmp_path: Path, failure) -> None:
    transformer = Mock(side_effect=["ONE", failure])
    app = create_app(Settings(data_dir=tmp_path), transformer)
    with TestClient(app) as client:
        response = client.post("/payload", json={"list_1": ["one"], "list_2": ["two"]})
        assert response.status_code == 502
        assert response.json() == {"detail": "Transformer service failed"}
        with Session(app.state.payload_service.engine) as session:
            assert session.scalar(select(func.count()).select_from(Transformation)) == 0
            assert session.scalar(select(func.count()).select_from(Payload)) == 0
    assert list((tmp_path / "payloads").iterdir()) == []


def test_missing_file_can_be_regenerated_from_cache(tmp_path: Path) -> None:
    transformer = Mock(side_effect=str.upper)
    body = {"list_1": ["hello"], "list_2": ["world"]}
    with TestClient(create_app(Settings(data_dir=tmp_path), transformer)) as client:
        created = client.post("/payload", json=body)
        file = tmp_path / "payloads" / f"{created.json()['id']}.json"
        file.unlink()
        assert client.get(created.headers["Location"]).status_code == 500
        repeated = client.post("/payload", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["id"] == created.json()["id"]
        assert client.get(created.headers["Location"]).json() == {"output": "HELLO, WORLD"}
        assert transformer.call_count == 2


def test_corrupted_file_is_detected(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        created = client.post("/payload", json={"list_1": ["hello"], "list_2": ["world"]})
        file = tmp_path / "payloads" / f"{created.json()['id']}.json"
        file.write_text('{"output":"CORRUPTED"}', encoding="utf-8")
        assert client.get(created.headers["Location"]).status_code == 500


def test_file_write_failure_does_not_publish_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cache_service import service

    def fail_write(*args, **kwargs):
        raise PermissionError("Disk unavailable")

    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(service.tempfile, "NamedTemporaryFile", fail_write)
        response = client.post("/payload", json={"list_1": ["one"], "list_2": ["two"]})
        assert response.status_code == 500
        with Session(app.state.payload_service.engine) as session:
            assert session.scalar(select(func.count()).select_from(Payload)) == 0
            assert session.scalar(select(func.count()).select_from(Transformation)) == 0


def test_writer_timeout_returns_retryable_error_and_reads_continue(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, sqlite_timeout=0.05))
    with TestClient(app) as client:
        created = client.post("/payload", json={"list_1": ["one"], "list_2": ["two"]})
        engine = app.state.payload_service.engine.execution_options(cache_writer=True)
        with engine.begin():
            # WAL keeps reads available while another worker holds the writer lock.
            assert client.get(created.headers["Location"]).status_code == 200
            blocked = client.post("/payload", json={"list_1": ["new"], "list_2": ["value"]})
            assert blocked.status_code == 503
            assert blocked.headers["Retry-After"] == "1"
