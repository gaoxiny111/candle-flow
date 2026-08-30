from typing import Optional

from app.schemas.common import BaseSchema


class FlowPointOut(BaseSchema):
    date: str
    time: str
    value: float


class FlowSeriesOut(BaseSchema):
    code: str
    name: str
    color: str
    latest: Optional[float] = None
    points: list[FlowPointOut]


class BroadFlowOut(BaseSchema):
    date: str
    updated_at: str
    series: list[FlowSeriesOut]
    partial: bool = False
    failed: list[str] = []
