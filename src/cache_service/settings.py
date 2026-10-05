from pathlib import Path
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CACHE_", extra="ignore")

    data_dir: Path = Path("data")
    sqlite_timeout: Annotated[float, Field(gt=0)] = 30.0
    transformer_version: Annotated[str, Field(min_length=1)] = "uppercase-v1"
