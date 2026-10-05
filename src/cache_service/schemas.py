from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

PayloadId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
InputString = Annotated[StrictStr, Field(max_length=10_000)]
InputList = Annotated[list[InputString], Field(max_length=1_000)]


class PayloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    list_1: InputList
    list_2: InputList

    @model_validator(mode="after")
    def equal_lengths(self) -> Self:
        if len(self.list_1) != len(self.list_2):
            raise ValueError("list_1 and list_2 must have the same length")
        return self


class PayloadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    output: str


class PayloadCreated(BaseModel):
    id: PayloadId
    message: str
    reused: bool
