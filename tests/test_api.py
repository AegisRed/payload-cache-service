from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from cache_service.api import create_app
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
