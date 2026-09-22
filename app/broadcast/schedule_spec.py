"""Step 2: typed 调度契约。"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator


class ImmediateSpec(BaseModel):
    mode: Literal["immediate"] = "immediate"


class AfterSpec(BaseModel):
    mode: Literal["after"] = "after"
    delay_seconds: float

    @field_validator("delay_seconds")
    @classmethod
    def _delay_seconds(cls, value: float) -> float:
        sec = float(value)
        if sec < 0:
            raise ValueError("delay_seconds 必须 >= 0")
        return sec


class AtSpec(BaseModel):
    mode: Literal["at"] = "at"
    run_at: str

    @field_validator("run_at")
    @classmethod
    def _run_at(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("run_at 不能为空")
        return text


class CronSpec(BaseModel):
    mode: Literal["cron"] = "cron"
    expr: str

    @field_validator("expr")
    @classmethod
    def _expr(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("expr 不能为空")
        return text


ScheduleSpec = Annotated[
    Union[ImmediateSpec, AfterSpec, AtSpec, CronSpec],
    Field(discriminator="mode"),
]


__all__ = [
    "ScheduleSpec",
    "ImmediateSpec",
    "AfterSpec",
    "AtSpec",
    "CronSpec",
]
