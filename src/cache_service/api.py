import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from cache_service.database import create_database
from cache_service.schemas import PayloadCreated, PayloadId, PayloadInput, PayloadOutput
from cache_service.service import (
    PayloadNotFound,
    PayloadService,
    PayloadStorageError,
    TransformationFailed,
)
from cache_service.settings import Settings
from cache_service.transformer import Transformer, uppercase

logger = logging.getLogger(__name__)


def get_service(request: Request) -> PayloadService:
    return request.app.state.payload_service


Service = Annotated[PayloadService, Depends(get_service)]


def create_app(settings: Settings | None = None, transformer: Transformer = uppercase) -> FastAPI:
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database(configuration.data_dir, configuration.sqlite_timeout)
        try:
            app.state.payload_service = PayloadService(
                engine, configuration.data_dir, transformer, configuration.transformer_version
            )
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Payload Cache Service", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(OperationalError)
    async def database_unavailable(_request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("Database operation failed", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable; retry later"},
            headers={"Retry-After": "1"},
        )

    @app.post("/payload", response_model=PayloadCreated, status_code=status.HTTP_201_CREATED)
    def create_payload(body: PayloadInput, response: Response, service: Service) -> PayloadCreated:
        try:
            payload = service.create(body)
        except TransformationFailed as exc:
            logger.exception("Transformer failed")
            raise HTTPException(status_code=502, detail="Transformer service failed") from exc
        except PayloadStorageError as exc:
            logger.exception("Payload storage failed")
            raise HTTPException(status_code=500, detail="Payload storage unavailable") from exc
        if payload.reused:
            response.status_code = status.HTTP_200_OK
        response.headers["Location"] = f"/payload/{payload.id}"
        return payload

    @app.get("/payload/{payload_id}", response_model=PayloadOutput)
    def read_payload(payload_id: PayloadId, service: Service) -> PayloadOutput:
        try:
            return service.read(payload_id)
        except PayloadNotFound as exc:
            raise HTTPException(status_code=404, detail="Payload not found") from exc
        except PayloadStorageError as exc:
            logger.exception("Payload storage failed")
            raise HTTPException(status_code=500, detail="Payload storage unavailable") from exc

    @app.get("/health", include_in_schema=False)
    def health(service: Service) -> dict[str, str]:
        with service.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
