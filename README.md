# Payload Cache Service

A FastAPI microservice that transforms two equally sized lists of strings, interleaves
their results, and saves the generated payload as a JSON file. SQLAlchemy and SQLite
persist both transformation results and payload metadata. A Pydantic Settings CLI
creates and reads payloads through the API.

## Run with Docker

Requires Docker with Compose:

```bash
docker compose up --build --detach --wait
```

The API is available at `http://127.0.0.1:8000`; interactive OpenAPI documentation is
at `http://127.0.0.1:8000/docs`. The container runs as an unprivileged user. The named
volume stores `/data/cache.sqlite3` and `/data/payloads/<id>.json` across restarts.

```bash
docker compose logs --follow
docker compose down
```

`down` preserves data. `docker compose down --volumes` deletes the service's data.
Set `CACHE_PORT` to use a different host port. The port binds to localhost by default.

## Run locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The committed lockfile fixes dependency versions for development and Docker builds.

```bash
uv sync --locked
uv run uvicorn cache_service.api:create_app --factory --host 127.0.0.1 --port 8000
```

Without uv, `python -m pip install .` installs the service and `cache-cli` entry point.
That installation resolves the version ranges in `pyproject.toml`; uv uses the lockfile.

## API

```bash
curl -X POST http://127.0.0.1:8000/payload \
  -H 'Content-Type: application/json' \
  --data-binary @examples/payload.json
```

A new payload returns HTTP **201**, a `Location` header, and:

```json
{
  "id": "<64-character SHA-256 identifier>",
  "message": "Payload created",
  "reused": false
}
```

An already generated output returns HTTP **200**, the same identifier, message
`Payload reused`, and `reused: true`. Read it using the returned identifier:

```bash
curl http://127.0.0.1:8000/payload/<id>
```

```json
{
  "output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"
}
```

Inputs must contain exactly `list_1` and `list_2`, both arrays of strings with equal
lengths. Empty lists and empty strings are valid; numbers are never coerced to strings.
Each list is limited to 1,000 items and each string to 10,000 characters. Strings retain
their whitespace, commas, and Unicode characters; transformation uses Python `str.upper`.

| Condition | HTTP status |
| --- | --- |
| New payload | 201 |
| Reused payload / successful read | 200 |
| Unknown, well-formed identifier | 404 |
| Invalid input or malformed identifier | 422 |
| Transformer failure | 502 |
| Database unavailable / writer lock timeout | 503, with `Retry-After: 1` |
| Missing, corrupt, or unwritable payload file | 500 |

`GET /health` checks database availability and supports the container health check.

## CLI

The actual argument parsing and validation use `pydantic-settings`' native CLI
support, with the same Pydantic payload model as the API. See the
[Pydantic Settings CLI documentation](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#command-line-support).

```text
cache-cli [-H|--host URL] [-r|--repeat N]
          [-i|--input FILE|- | -j|--json JSON]
          [-o|--output FILE|-] [-h|--help]
```

The task assigns `-h` to both host and help. To resolve that conflict, this CLI uses
`-H` for host and reserves `-h` for help. All specified long options are supported.

```bash
# Use the sample file and verify cache reuse across three POST/GET pairs.
uv run cache-cli --host http://127.0.0.1:8000 --repeat 3 --input examples/payload.json

# Inline JSON (POSIX shell).
uv run cache-cli -j '{"list_1":["hello"],"list_2":["world"]}'

# Read stdin and save newline-delimited JSON results.
cat examples/payload.json | uv run cache-cli -i - -o results.jsonl

uv run cache-cli --help
```

PowerShell file/stdin examples avoid shell-dependent JSON escaping:

```powershell
uv run cache-cli -H http://127.0.0.1:8000 -r 3 -i examples/payload.json
Get-Content -Raw examples/payload.json | uv run cache-cli -i - -o results.jsonl
```

The CLI is also installed inside the image and can test the running service directly:

```bash
docker compose exec api cache-cli -j '{"list_1":["hello"],"list_2":["world"]}' -r 3
```

`--input` and `--json` are mutually exclusive. Omitting both reads stdin. Output defaults
to stdout; diagnostics go to stderr. Each iteration makes one POST and one GET and
writes one JSON line containing `iteration`, `id`, `reused`, and `output`. Repeat must
be positive; the host must be an HTTP(S) URL. Requests have a 30-second timeout. CLI
options are read from command-line arguments rather than environment variables.

Exit codes: **0** for success/help, **2** for invalid arguments or input, **1** for
HTTP, server-response, or output errors. The CLI stops on the first failure. An output
file is overwritten, and completed iterations remain available if a later one fails.

## Cache design and tradeoffs

1. Deduplicate exact input strings across both lists. Fetch existing results in
   batches, and call the transformer only for missing `(transformer_version, source)`
   keys. Preserve every occurrence and its position in the final output.
2. Reserve SQLite's writer lock **before** checking misses, using `BEGIN IMMEDIATE`.
   This prevents duplicate calls across threads and independent worker processes
   sharing the same database. Schema creation uses the same lock. Initial WAL setup
   retries transient contention within the configured timeout. WAL allows reads to
   continue while a creator is working.
3. Hash the canonical UTF-8 JSON output using SHA-256. Identical generated payloads
   share an identifier, even when different inputs produce the same output. Database
   primary keys enforce uniqueness; identifiers are independent of request timing.
4. Write a temporary file, flush it, and atomically rename it before committing the
   cache and payload metadata in one database transaction. Readers only access
   registered identifiers and verify the stored file's checksum. A repeated POST can
   restore a missing payload file using cached transformations.

SQLite deliberately serializes creators, including their transformer calls. This keeps
the implementation small and avoids duplicate calls under normal operation, at the
cost of write throughput for a slow external service. It suits a single-host service
with a shared local data directory. A distributed deployment would need a different
coordination and storage design; PostgreSQL is not implemented because SQLite satisfies
the task's database requirement.

The supplied transformer is a deterministic, side-effect-free function. It can be
replaced through the application factory's `transformer` argument. Change
`CACHE_TRANSFORMER_VERSION` whenever its behavior changes so stale results are not reused.
Older generated files remain readable. No expiration or eviction is implemented because
the task requires persistent reuse and gives no retention policy.

An external call and a database commit cannot form a single atomic operation: a failed
request rolls back its new cache entries, so a retry may repeat calls that happened
before failure. A crash between file creation and database commit may leave an
unreferenced file; it is never served and a later matching request safely replaces it.
This is not an exactly-once guarantee across failures. Back up the whole data directory
consistently, including both SQLite and payload files. Schema creation is automatic;
future schema changes would need migrations.

## Configuration

The service reads these environment variables with Pydantic Settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_DATA_DIR` | `data` locally, `/data` in Docker | Database and payload directory |
| `CACHE_SQLITE_TIMEOUT` | `30` | Positive SQLite writer-lock timeout in seconds |
| `CACHE_TRANSFORMER_VERSION` | `uppercase-v1` | Nonempty transformation cache namespace |

## Tests and CI

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=cache_service --cov-report=term-missing --cov-fail-under=90
```

Tests cover the supplied example, interleaving, cache hits and partial hits, duplicate
strings, content-based identifiers, validation, empty and Unicode inputs, persistence
across restart, transformer version changes, transaction rollback, disk failures,
checksum verification, writer timeouts, concurrent startup and requests in independent
processes, and the CLI's arguments, files, stdin, output, and API integration.

GitHub Actions runs linting and tests on Linux with Python 3.11/3.12 and Windows with
Python 3.11, then builds and starts the Docker image and exercises it using `cache-cli`.
Both actions and the uv version are pinned. Production image dependencies exclude test
and lint tools.
