import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cache_service import cli
from cache_service.api import create_app
from cache_service.cli import CliSettings, load_input, run
from cache_service.settings import Settings

BODY = {"list_1": ["hello"], "list_2": ["world"]}


def test_pydantic_settings_parses_short_options() -> None:
    settings = CliSettings(
        _cli_parse_args=["-H", "http://localhost:8000", "-r", "3", "-j", json.dumps(BODY)]
    )
    assert settings.repeat == 3
    assert settings.host.host == "localhost"
    assert load_input(settings, io.StringIO()).model_dump() == BODY


@pytest.mark.parametrize(
    "arguments",
    [
        ["--repeat", "0"],
        ["-r", "-1"],
        ["--repeat", "bad"],
        ["--host", "ftp://localhost"],
        ["--host", "http://localhost/?q=1"],
        ["--host", "http://user:password@localhost"],
        ["-i", "-", "-j", json.dumps(BODY)],
        ["-o", ""],
    ],
)
def test_options_are_validated(arguments: list[str]) -> None:
    with pytest.raises(ValidationError):
        CliSettings(_cli_parse_args=arguments)


def test_file_and_stdin_input(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps(BODY), encoding="utf-8-sig")
    from_file = load_input(CliSettings(_cli_parse_args=["-i", str(path)]), io.StringIO())
    from_stdin = load_input(CliSettings(_cli_parse_args=["-i", "-"]), io.StringIO(json.dumps(BODY)))
    default_stdin = load_input(CliSettings(_cli_parse_args=[]), io.StringIO(json.dumps(BODY)))
    assert from_file == from_stdin == default_stdin


def test_repeat_creates_and_reads_against_api(tmp_path: Path) -> None:
    settings = CliSettings(_cli_parse_args=["--repeat", "3"])
    payload = load_input(settings, io.StringIO(json.dumps(BODY)))
    output = io.StringIO()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        run(settings, payload, client, output)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["iteration"] for item in records] == [1, 2, 3]
    assert [item["reused"] for item in records] == [False, True, True]
    assert len({item["id"] for item in records}) == 1
    assert all(item["output"] == "HELLO, WORLD" for item in records)


def mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client_class = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_main_writes_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload_id = "a" * 64
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            assert json.loads(request.content) == BODY
            return httpx.Response(
                201, json={"id": payload_id, "message": "Payload created", "reused": False}
            )
        return httpx.Response(200, json={"output": "HELLO, WORLD"})

    mock_client(monkeypatch, handler)
    destination = tmp_path / "result.jsonl"
    assert cli.main(["-j", json.dumps(BODY), "-o", str(destination)]) == 0
    record = json.loads(destination.read_text(encoding="utf-8"))
    assert record["id"] == payload_id
    assert record["output"] == "HELLO, WORLD"
    assert paths == ["/payload", f"/payload/{payload_id}"]


def test_main_rejects_invalid_input_before_network(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["-j", '{"list_1": ["one"], "list_2": []}']) == 2
    assert "same length" in capsys.readouterr().err
    assert cli.main(["--unknown"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_main_reports_http_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    mock_client(monkeypatch, lambda request: httpx.Response(503, json={"detail": "Unavailable"}))
    assert cli.main(["-j", json.dumps(BODY)]) == 1
    captured = capsys.readouterr()
    assert "503" in captured.err
    assert captured.out == ""


def test_help_uses_lowercase_h(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["-h"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--host" in help_text
    assert "-H" in help_text
