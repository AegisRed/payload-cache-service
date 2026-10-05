import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Self, TextIO

import httpx
from pydantic import AliasChoices, Field, HttpUrl, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from cache_service.schemas import PayloadCreated, PayloadInput, PayloadOutput


class CliSettings(BaseSettings):
    """Create and read a payload repeatedly, writing one JSON result per iteration."""

    model_config = SettingsConfigDict(
        cli_parse_args=True,
        cli_exit_on_error=False,
        cli_prog_name="cache-cli",
        cli_hide_none_type=True,
        populate_by_name=True,
        case_sensitive=True,
    )

    host: HttpUrl = Field(
        default=HttpUrl("http://127.0.0.1:8000"),
        validation_alias=AliasChoices("host", "H"),
        description="Server base URL. Use -H because -h is reserved for help.",
    )
    repeat: Annotated[int, Field(gt=0)] = Field(
        default=1, validation_alias=AliasChoices("repeat", "r")
    )
    input_file: str | None = Field(
        default=None,
        validation_alias=AliasChoices("input", "i"),
        description="UTF-8 JSON file, or - for stdin (default when --json is absent).",
    )
    json_input: str | None = Field(
        default=None,
        validation_alias=AliasChoices("json", "j"),
        description="Inline JSON; mutually exclusive with --input.",
    )
    output_file: str = Field(
        default="-",
        validation_alias=AliasChoices("output", "o"),
        description="Output file, or - for stdout. Format: newline-delimited JSON.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # CLI options should not silently change because of unrelated shell variables.
        return (init_settings,)

    @model_validator(mode="after")
    def sanitize_options(self) -> Self:
        for field in ("input_file", "json_input"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError("Explicit input options must not be null")
        if self.input_file is not None and self.json_input is not None:
            raise ValueError("--input and --json are mutually exclusive")
        if self.host.query or self.host.fragment or self.host.username or self.host.password:
            raise ValueError("--host must not contain credentials, a query, or a fragment")
        if self.input_file == "" or self.output_file == "":
            raise ValueError("Input and output file names must not be empty")
        return self


def load_input(settings: CliSettings, stdin: TextIO) -> PayloadInput:
    if settings.json_input is not None:
        raw = settings.json_input
    elif settings.input_file in (None, "-"):
        raw = stdin.read()
    else:
        raw = Path(settings.input_file).read_text(encoding="utf-8-sig")
    return PayloadInput.model_validate_json(raw.removeprefix("\ufeff"))


def run(settings: CliSettings, payload: PayloadInput, client: httpx.Client, output: TextIO) -> None:
    for iteration in range(1, settings.repeat + 1):
        response = client.post("payload", json=payload.model_dump())
        response.raise_for_status()
        created = PayloadCreated.model_validate(response.json())
        response = client.get(f"payload/{created.id}")
        response.raise_for_status()
        result = PayloadOutput.model_validate(response.json())
        record = {
            "iteration": iteration,
            "id": created.id,
            "reused": created.reused,
            "output": result.output,
        }
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        settings = CliSettings(_cli_parse_args=sys.argv[1:] if argv is None else argv)
        payload = load_input(settings, sys.stdin)
    except (ValidationError, SettingsError, OSError, ValueError) as exc:
        print(f"cache-cli: {exc}", file=sys.stderr)
        return 2

    try:
        with ExitStack() as stack:
            output = (
                sys.stdout
                if settings.output_file == "-"
                else stack.enter_context(
                    Path(settings.output_file).open("w", encoding="utf-8", newline="\n")
                )
            )
            client = stack.enter_context(
                httpx.Client(base_url=str(settings.host).rstrip("/") + "/", timeout=30.0)
            )
            run(settings, payload, client, output)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        print(f"cache-cli: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
