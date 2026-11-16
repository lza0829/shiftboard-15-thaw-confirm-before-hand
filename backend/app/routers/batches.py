from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services import handover as svc

router = APIRouter(prefix="/batches", tags=["在途批次"])


class BatchActionBody(BaseModel):
    operator_shift_id: int = Field(..., description="操作班次，须为批次当前责任班次")
    reason: str = Field(default="", description="操作原因，留审计")


@router.get("")
def get_batches(owner_shift_id: int | None = Query(default=None)):
    return svc.list_batches(owner_shift_id)


@router.get("/{batch_id}/events")
def get_batch_events(batch_id: int):
    return svc.list_batch_events(batch_id)


@router.post("/{batch_id}/freeze")
def freeze(batch_id: int, body: BatchActionBody):
    return svc.change_batch_status(batch_id, "freeze", body.operator_shift_id, body.reason)


@router.post("/{batch_id}/unfreeze")
def unfreeze(batch_id: int, body: BatchActionBody):
    # 解冻只落「待确认归还」，绝不直接变可交班在途
    return svc.change_batch_status(batch_id, "unfreeze", body.operator_shift_id, body.reason)


@router.post("/{batch_id}/confirm-return")
def confirm_return(batch_id: int, body: BatchActionBody):
    # 确认归还：必须由当前责任班次执行，确认后才回到可交班在途
    return svc.change_batch_status(batch_id, "confirm_return", body.operator_shift_id, body.reason)


@router.post("/{batch_id}/refreeze")
def refreeze(batch_id: int, body: BatchActionBody):
    return svc.change_batch_status(batch_id, "refreeze", body.operator_shift_id, body.reason)


@router.post("/{batch_id}/scrap")
def scrap(batch_id: int, body: BatchActionBody):
    return svc.change_batch_status(batch_id, "scrap", body.operator_shift_id, body.reason)


@router.post("/{batch_id}/settle")
def settle(batch_id: int, body: BatchActionBody):
    return svc.change_batch_status(batch_id, "settle", body.operator_shift_id, body.reason)
