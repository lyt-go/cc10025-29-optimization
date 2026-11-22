"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    BatchConfirmPayload,
    BatchOpenPayload,
    BatchRecalculatePayload,
    EntryPayload,
    PageResult,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


# ---------- 批量核算底稿 ----------
# 固定路径必须注册在 /{module} 之前，否则会被动态模块名抢先匹配。
@router.get("/cost_tracker/batches")
def list_cost_batches() -> dict[str, Any]:
    service = require_service("cost_tracker")
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.post("/cost_tracker/batches")
def open_cost_batch(payload: BatchOpenPayload) -> ActionResult:
    service = require_service("cost_tracker")
    try:
        batch = service.open_batch(
            payload.entry_ids,
            label=payload.label,
            variance_policy=payload.variance_policy,
            decisions=payload.decisions,
        )
    except ValueError as error:
        return ActionResult(ok=False, message=str(error))
    totals = batch["totals"]
    return ActionResult(
        ok=True,
        message=(f"批量核算底稿 {batch['batch_id']} 已生成，"
                 f"整组合计 {totals['current_text']}，"
                 f"本次入账 {totals['last_applied_text']}，"
                 f"结转下一批 {totals['carried_text']}"),
        entry=batch,
    )


@router.get("/cost_tracker/batches/{batch_id}")
def get_cost_batch(batch_id: str) -> dict[str, Any]:
    service = require_service("cost_tracker")
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批量核算底稿 {batch_id} 不存在")
    return batch


@router.post("/cost_tracker/batches/{batch_id}/recalculate")
def recalculate_cost_batch(batch_id: str, payload: BatchRecalculatePayload) -> ActionResult:
    service = require_service("cost_tracker")
    try:
        batch = service.recalculate_batch(
            batch_id,
            confirm_entry_ids=payload.confirm_entry_ids,
            decisions=payload.decisions,
            variance_policy=payload.variance_policy,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    totals = batch["totals"]
    return ActionResult(
        ok=True,
        message=(f"底稿 {batch_id} 第 {batch['recalc_count']} 次重算完成，"
                 f"整组合计 {totals['current_text']}，"
                 f"本次新增入账 {totals['last_applied_text']}，"
                 f"结转下一批 {totals['carried_text']}"),
        entry=batch,
    )


@router.post("/cost_tracker/batches/{batch_id}/confirm")
def confirm_cost_batch(batch_id: str, payload: BatchConfirmPayload) -> ActionResult:
    service = require_service("cost_tracker")
    try:
        batch = service.confirm_batch(batch_id, payload.entry_ids)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return ActionResult(
        ok=True,
        message=f"底稿 {batch_id} 已确认 {len(payload.entry_ids)} 条成本记录",
        entry=batch,
    )


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    # 把动作参数交给领域服务（例如补数完工的实测用量）；是否消费由服务决定。
    if hasattr(service, "set_action_values"):
        service.set_action_values(payload.values)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
