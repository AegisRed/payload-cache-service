import hashlib
import os
import tempfile
from itertools import chain
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cache_service.models import Payload, Transformation
from cache_service.schemas import PayloadCreated, PayloadInput, PayloadOutput
from cache_service.transformer import Transformer


class PayloadNotFound(Exception):
    pass


class TransformationFailed(Exception):
    pass


class PayloadStorageError(Exception):
    pass


class PayloadService:
    def __init__(
        self, engine: Engine, data_dir: Path, transformer: Transformer, version: str
    ) -> None:
        self.engine = engine
        self.payload_dir = data_dir / "payloads"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.transformer = transformer
        self.version = version

    def create(self, request: PayloadInput) -> PayloadCreated:
        # SQLite's writer lock coordinates separate threads, workers, and service
        # instances sharing the database. A second creator sees committed results.
        with Session(self.engine.execution_options(cache_writer=True)) as session:
            sources = list(dict.fromkeys(chain(request.list_1, request.list_2)))
            results: dict[str, str] = {}
            # Stay below SQLite's bind-parameter limit even on older builds.
            for offset in range(0, len(sources), 400):
                cached = session.scalars(
                    select(Transformation).where(
                        Transformation.version == self.version,
                        Transformation.source.in_(sources[offset : offset + 400]),
                    )
                )
                results.update((item.source, item.result) for item in cached)

            # An empty request must still acquire the lock before touching files.
            session.connection()
            for source in sources:
                if source in results:
                    continue
                try:
                    result = self.transformer(source)
                    if not isinstance(result, str):
                        raise TypeError("Transformer must return a string")
                except Exception as exc:
                    raise TransformationFailed from exc
                results[source] = result
                session.add(Transformation(version=self.version, source=source, result=result))

            output = PayloadOutput(
                output=", ".join(
                    results[value]
                    for pair in zip(request.list_1, request.list_2, strict=True)
                    for value in pair
                )
            )
            encoded = output.model_dump_json().encode("utf-8")
            payload_id = hashlib.sha256(encoded).hexdigest()
            reused = session.get(Payload, payload_id) is not None
            path = self.payload_dir / f"{payload_id}.json"
            if not reused or not path.is_file():
                self._write_file(path, encoded)
            if not reused:
                session.add(Payload(id=payload_id))
            # Publish the complete file before committing its metadata. A crash
            # can leave an unreferenced file, but never a visible partial payload.
            session.commit()

        return PayloadCreated(
            id=payload_id,
            message="Payload reused" if reused else "Payload created",
            reused=reused,
        )

    def read(self, payload_id: str) -> PayloadOutput:
        with Session(self.engine) as session:
            if session.get(Payload, payload_id) is None:
                raise PayloadNotFound
        try:
            encoded = (self.payload_dir / f"{payload_id}.json").read_bytes()
            if hashlib.sha256(encoded).hexdigest() != payload_id:
                raise ValueError("Payload checksum mismatch")
            return PayloadOutput.model_validate_json(encoded)
        except (OSError, ValueError) as exc:
            raise PayloadStorageError from exc

    @staticmethod
    def _write_file(path: Path, encoded: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise PayloadStorageError from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
