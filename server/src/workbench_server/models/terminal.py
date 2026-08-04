"""Terminal WebSocket protocol.

Client -> server: discriminated union on ``type`` (input | resize).
Server -> client: output chunks and a final exit notice. All frames are JSON text.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class TerminalInput(BaseModel):
    type: Literal["input"]
    data: str


class TerminalResize(BaseModel):
    type: Literal["resize"]
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=2, le=1000)


TerminalClientMessage = Annotated[TerminalInput | TerminalResize, Field(discriminator="type")]

terminal_client_message: TypeAdapter[TerminalInput | TerminalResize] = TypeAdapter(
    TerminalClientMessage
)


class TerminalOutput(BaseModel):
    type: Literal["output"] = "output"
    data: str


class TerminalExit(BaseModel):
    type: Literal["exit"] = "exit"
