from fastapi import APIRouter

from app.core.exceptions import DataSourceError
from app.schemas.common import ApiResponse
from app.schemas.flow import BroadFlowOut
from app.services.fund_flow_client import fetch_broad_index_flow

router = APIRouter()


@router.get("/flow/broad")
def broad_index_flow():
    data = fetch_broad_index_flow()
    if not any(s.get("points") for s in data.get("series") or []):
        raise DataSourceError("无法获取宽基主力分时资金，请检查网络后重试")
    return ApiResponse(data=BroadFlowOut.model_validate(data))
