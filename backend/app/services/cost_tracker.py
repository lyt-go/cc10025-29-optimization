"""cost_tracker 领域服务：成本追踪管理，接收 asset_catalog/cold_storage/hot_prefetch/backfill_job 上游事件。

跨模块数据流：
    asset_catalog → cost_tracker: 资产发布创建成本种子
    cold_storage → cost_tracker: 冷存迁移更新存储成本
    hot_prefetch → cost_tracker: 预热回迁更新传输成本
    partition_plan → backfill_job → cost_tracker: 补数完工提供已确认计量
    backfill_job → cost_tracker → 批量核算底稿: 多选资产一次采样批量重算

核算口径：
    - 单条核算与批量底稿共用同一套采样器（BatchIndex），三个计费项按
      同一采样位置取值，避免逐条确认时反复扫描冷存/补数/回迁作业；
    - 每笔核算保留实际采用的用量、计量修订与确认时间，随差额一起落库。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.cost_batch import batch_service
from app.services.measurement import ITEM_LABELS, money
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class CostTrackerService(BaseModuleService):
    MODULE = "cost_tracker"
    ENTITY = "成本记录"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["记录编号", "关联资产", "存储成本", "计算成本", "传输成本", "统计周期",
                   "入账批次", "趋势方向", "异常标记"]
    REQUIRED_FIELDS = ["记录编号", "关联资产", "存储成本"]
    STATUS_ORDER = ["草稿", "已核算", "已确认", "已归档"]
    ACTION_RULES = {"提交核算": "已核算", "确认成本": "已确认", "标记归档": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 单条核算 ----------
    def calculate_cost(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """核算成本：一次采样取三项用量，复用同一份作业索引。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        sampling_point = f"SP-SINGLE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        index = self._build_index([asset_id])
        snapshot = index.sample(asset_id, sampling_point)
        self._apply_snapshot(entry, snapshot)

        entry["核算采样位置"] = sampling_point
        entry["核算依据"] = {
            key: {
                "job_id": item["job_id"],
                "job_no": item["job_no"],
                "usage": item["usage_text"],
                "amount": item["amount"],
                "basis": item["basis"],
                "confirmed_at": item["confirmed_at"],
                "revision": int(item.get("revision") or 0),
            }
            for key, item in snapshot["items"].items()
        }
        entry["status"] = "已核算"
        entry["pending"] = False

        self._write_cost_report(asset_id, entry)
        self._write_single_batch(asset_id, entry, snapshot)

        return entry, f"{self.ENTITY} {entry_id} 已核算，总成本 {snapshot['total']:.2f}"

    def get_cost_report(self, asset_id: str) -> dict[str, Any] | None:
        """读取某资产的成本报告。"""
        filename = f"cost_report_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交核算":
            return self.calculate_cost(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 批量核算底稿 ----------
    def open_batch(self, entry_ids: list[int], **options: Any) -> dict[str, Any]:
        return batch_service().open_batch(entry_ids, **options)

    def recalculate_batch(self, batch_id: str, **options: Any) -> dict[str, Any]:
        return batch_service().recalculate(batch_id, **options)

    def confirm_batch(self, batch_id: str, entry_ids: list[int]) -> dict[str, Any]:
        return batch_service().confirm(batch_id, entry_ids)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return batch_service().get_batch(batch_id)

    def list_batches(self) -> list[dict[str, Any]]:
        return batch_service().list_batches()

    # ---------- 共享采样 ----------
    def _build_index(self, asset_ids: list[str]):
        from app.services import service_for

        backfill = service_for("backfill_job")
        measurements = backfill.confirmed_measurements(asset_ids) if backfill else []
        from app.services.cost_batch import BatchIndex

        return BatchIndex(asset_ids, measurements)

    def _apply_snapshot(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        items = snapshot["items"]
        entry["存储成本"] = money(items["storage"]["amount"])
        entry["计算成本"] = money(items["compute"]["amount"])
        entry["传输成本"] = money(items["transfer"]["amount"])
        total = snapshot["total"]
        entry["趋势方向"] = "上升" if total > 100 else "持平"
        entry["异常标记"] = "异常" if total > 500 else "无"
        entry["核算总成本"] = money(total)
        entry["核算采样位置"] = snapshot["sampling_point"]
        # 逐项保留实际采用的用量与确认时间，供盘点解释合计。
        entry["实际采用用量"] = {
            key: {
                "label": ITEM_LABELS[key],
                "usage": item["usage_text"],
                "amount": money(item["amount"]),
                "basis": item["basis"],
                "confirmed_at": item["confirmed_at"],
                "revision": int(item.get("revision") or 0),
            }
            for key, item in items.items()
        }

    def _write_single_batch(self, asset_id: str, entry: dict[str, Any],
                            snapshot: dict[str, Any]) -> None:
        """单条核算也留一份可对照底稿，便于与批量底稿比较读取情况。"""
        record = {
            "batch_id": f"SINGLE-{entry.get('id')}",
            "mode": "单条核算",
            "asset_id": asset_id,
            "entry_id": int(entry.get("id", 0)),
            "sampling_point": snapshot["sampling_point"],
            "read_count": 1,
            "assets": [{
                "asset_id": asset_id,
                "baseline": snapshot,
                "current": snapshot,
                "items": list(snapshot["items"].values()),
                "total": snapshot["total"],
            }],
            "totals": {"current": snapshot["total"]},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        filename = f"cost_single_{entry.get('记录编号', entry.get('id'))}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _write_cost_report(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"cost_report_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "record_id": str(entry.get("记录编号", "")),
            "storage_cost": str(entry.get("存储成本", "")),
            "compute_cost": str(entry.get("计算成本", "")),
            "transfer_cost": str(entry.get("传输成本", "")),
            "period": str(entry.get("统计周期", "")),
            "trend": str(entry.get("趋势方向", "")),
            "anomaly": str(entry.get("异常标记", "")),
            "sampling_point": str(entry.get("核算采样位置", "")),
            "accounting_basis": entry.get("核算依据", {}),
            "applied_usage": entry.get("实际采用用量", {}),
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)
