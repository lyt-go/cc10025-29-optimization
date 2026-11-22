"""批量核算底稿：多选资产一次采样、一次读取、按修订版本比较差额并幂等入账。

设计要点
--------
1. **一次采样位置**：开批时对选中资产做一次采样，三个计费项（存储/计算/传输）
   共享同一个 `sampling_point` 与同一份源作业快照，保证可按同一位置复算。
2. **复用读取结果**：一次遍历 cold_storage / backfill_job / hot_prefetch 建立索引，
   批量里的资产不再各自重复扫描；补数计量由补数服务统一提供已确认版本。
3. **修订版本对照**：补数作业每次完工/修订都会产生 `revision`。底稿同时保留
   开批基线（baseline）与本次采样（current），逐项给出用量、金额与差额。
4. **幂等入账**：台账按 (批次, 资产, 计费项) 记录累计已入账金额，重算只补
   “应计 - 已入账”的差额；重复执行差额为 0，不会二次入账。
5. **差额去向**：默认本次入账；已确认资产在确认后发生的修订自动结转下一批，
   也可以在开批/重算时逐资产指定，底稿里保留决策与理由。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.measurement import (
    ITEM_LABELS,
    ITEM_RATES,
    ITEM_SOURCE_MODULES,
    amount_for,
    derived_kind,
    format_gb,
    money,
    parse_gb,
)
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")
DECISION_NOW = "本次入账"
DECISION_NEXT = "结转下一批"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BatchIndex:
    """一次读取形成的源作业索引，供同一批次内所有资产复用。"""

    def __init__(self, asset_ids: list[str],
                 backfill_measurements: list[dict[str, Any]]) -> None:
        wanted = list(dict.fromkeys(asset_ids))
        self.cold_by_asset: dict[str, dict[str, Any]] = {}
        for row in store.rows("cold_storage"):
            asset = str(row.get("源资产", ""))
            if asset in wanted and asset not in self.cold_by_asset:
                self.cold_by_asset[asset] = row

        # 回迁任务没有直接的资产字段，用冷存位置/目标分区一次性匹配全部选中资产。
        self.hot_by_asset: dict[str, dict[str, Any]] = {}
        for row in store.rows("hot_prefetch"):
            location = str(row.get("冷存位置", ""))
            partition = str(row.get("目标分区", ""))
            for asset in wanted:
                if asset in location or asset in partition:
                    self.hot_by_asset.setdefault(asset, row)

        self.backfill_by_asset: dict[str, dict[str, Any]] = {}
        for measurement in backfill_measurements:
            asset = str(measurement.get("asset_id", ""))
            if not asset:
                continue
            current = self.backfill_by_asset.get(asset)
            if current is None or int(measurement.get("revision") or 0) >= int(
                current.get("revision") or 0
            ):
                self.backfill_by_asset[asset] = measurement

        self.read_count = 1
        self.stats = {
            "cold_storage": len(store.rows("cold_storage")),
            "hot_prefetch": len(store.rows("hot_prefetch")),
            "backfill_job": len(backfill_measurements),
            "backfill_measurement_calls": 1,
        }

    def sample(self, asset_id: str, sampling_point: str) -> dict[str, Any]:
        items: dict[str, dict[str, Any]] = {}
        items["storage"] = self._sample_storage(asset_id, sampling_point)
        items["compute"] = self._sample_compute(asset_id, sampling_point)
        items["transfer"] = self._sample_transfer(asset_id, sampling_point)
        total = round(sum(item["amount"] for item in items.values()), 2)
        return {
            "asset_id": asset_id,
            "sampling_point": sampling_point,
            "sampled_at": _now(),
            "items": items,
            "total": total,
        }

    # ---------- 三个计费项 ----------
    def _sample_storage(self, asset_id: str, sampling_point: str) -> dict[str, Any]:
        row = self.cold_by_asset.get(asset_id)
        if row is None:
            return self._item("storage", None, "无冷存作业，沿用兜底金额", False,
                              sampling_point, amount=5.0)
        usage = str(row.get("迁移量", ""))
        usage_gb = parse_gb(usage)
        measured = usage_gb is not None
        return self._item(
            "storage", row, "冷存迁移量", measured, sampling_point,
            usage=usage, usage_gb=usage_gb,
            amount=amount_for("storage", usage_gb, measured=measured),
            confirmed_at=str(row.get("updated_at") or row.get("完成时间") or ""),
        )

    def _sample_compute(self, asset_id: str, sampling_point: str) -> dict[str, Any]:
        measurement = self.backfill_by_asset.get(asset_id)
        if measurement is None:
            return self._item("compute", None, "无补数作业，沿用兜底金额", False,
                              sampling_point, amount=10.0)
        usage = str(measurement.get("usage") or "")
        usage_gb = measurement.get("usage_gb")
        if usage_gb is None:
            usage_gb = parse_gb(usage)
        measured = usage_gb is not None
        item = self._item(
            "compute", measurement, "补数已确认计量", measured, sampling_point,
            usage=usage, usage_gb=usage_gb,
            amount=amount_for("compute", usage_gb, measured=measured),
            confirmed_at=str(measurement.get("confirmed_at") or ""),
        )
        item.update({
            "job_id": int(measurement.get("job_id") or 0),
            "job_no": str(measurement.get("job_no") or ""),
            "revision": int(measurement.get("revision") or 1),
            "measurement_kind": str(measurement.get("kind") or ""),
            "source": str(measurement.get("source") or ""),
            "usage_text": str(measurement.get("usage_text") or usage),
        })
        return item

    def _sample_transfer(self, asset_id: str, sampling_point: str) -> dict[str, Any]:
        row = self.hot_by_asset.get(asset_id)
        if row is None:
            return self._item("transfer", None, "无回迁作业，沿用兜底金额", False,
                              sampling_point, amount=3.0)
        usage = str(row.get("回迁量", ""))
        usage_gb = parse_gb(usage)
        measured = usage_gb is not None
        return self._item(
            "transfer", row, "预热回迁量", measured, sampling_point,
            usage=usage, usage_gb=usage_gb,
            amount=amount_for("transfer", usage_gb, measured=measured),
            confirmed_at=str(row.get("updated_at") or row.get("完成时间") or ""),
        )

    def _item(
        self,
        item: str,
        source: dict[str, Any] | None,
        basis: str,
        measured: bool,
        sampling_point: str,
        *,
        usage: str = "",
        usage_gb: float | None = None,
        amount: float = 0.0,
        confirmed_at: str = "",
    ) -> dict[str, Any]:
        job_id = 0
        job_no = ""
        if source is not None:
            job_id = int(source.get("id") or source.get("job_id") or 0)
            job_no = str(source.get("任务编号") or source.get("作业编号")
                         or source.get("job_no") or "")
        return {
            "item": item,
            "label": ITEM_LABELS[item],
            "source_module": ITEM_SOURCE_MODULES[item],
            "job_id": job_id,
            "job_no": job_no,
            "usage": usage,
            "usage_gb": usage_gb,
            "usage_text": usage or format_gb(usage_gb),
            "measured": measured,
            "amount": round(amount, 2),
            "amount_text": money(amount),
            "basis": basis,
            "kind": derived_kind("已完成" if measured else "执行中"),
            "confirmed_at": confirmed_at,
            "sampling_point": sampling_point,
            "rate": ITEM_RATES[item],
        }


class CostBatchService:
    """成本批量核算底稿的领域实现，由 CostTrackerService 暴露给路由层。"""

    PREFIX = "BATCH"

    def __init__(self) -> None:
        self._batches: dict[str, dict[str, Any]] = {}
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 开批 ----------
    def open_batch(
        self,
        entry_ids: list[int],
        *,
        label: str = "",
        variance_policy: str = DECISION_NOW,
        decisions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        entries = self._resolve_entries(entry_ids)
        if not entries:
            raise ValueError("请选择至少一条成本记录")

        batch_id = self._next_batch_id()
        sampling_point = f"SP-{batch_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        index = self._build_index([str(e["关联资产"]) for e in entries])
        snapshots = {
            str(entry.get("关联资产")): index.sample(str(entry.get("关联资产")), sampling_point)
            for entry in entries
        }

        assets: list[dict[str, Any]] = []
        for entry in entries:
            asset_id = str(entry.get("关联资产", ""))
            snapshot = snapshots[asset_id]
            asset = self._asset_sheet(entry, snapshot, variance_policy, decisions or {})
            assets.append(asset)

        batch = {
            "batch_id": batch_id,
            "label": label or f"批量核算 {batch_id}",
            "status": "打开",
            "opened_at": _now(),
            "closed_at": "",
            "sampling_point": sampling_point,
            "sampling_note": "开批一次性采样，三计费项共享同一作业快照",
            "read_count": index.read_count,
            "read_stats": index.stats,
            "variance_policy": variance_policy,
            "assets": assets,
            "ledger": [],
            "recalc_count": 0,
            "recalculated_at": "",
            "last_action": "开批",
            "totals": {},
            "confirmation_times": [],
        }
        # 开批即把基线入账，后续重算只补差额。
        self._apply_accounting(batch, stage="开批")
        self._batches[batch_id] = batch
        self._persist(batch)
        return batch

    # ---------- 重算 / 复核 ----------
    def recalculate(
        self,
        batch_id: str,
        *,
        confirm_entry_ids: list[int] | None = None,
        decisions: dict[str, str] | None = None,
        variance_policy: str | None = None,
    ) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        if variance_policy:
            batch["variance_policy"] = variance_policy

        if confirm_entry_ids:
            self._confirm(batch, confirm_entry_ids)

        asset_ids = [str(asset["asset_id"]) for asset in batch["assets"]]
        index = self._build_index(asset_ids)
        sampling_point = f"SP-{batch_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        for asset in batch["assets"]:
            current = index.sample(str(asset["asset_id"]), sampling_point)
            self._refresh_asset(asset, current, decisions or {}, batch["variance_policy"])
            asset["latest_sampling_point"] = sampling_point

        batch["sampling_point"] = sampling_point
        batch["read_count"] = index.read_count
        batch["read_stats"] = index.stats
        batch["recalc_count"] = int(batch.get("recalc_count", 0)) + 1
        batch["recalculated_at"] = _now()
        batch["last_action"] = "重算"
        self._apply_accounting(batch, stage="重算")
        self._persist(batch)
        return batch

    def confirm(
        self, batch_id: str, entry_ids: list[int],
    ) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        self._confirm(batch, entry_ids)
        batch["last_action"] = "确认"
        self._apply_accounting(batch, stage="确认")
        self._persist(batch)
        return batch

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._batches.get(batch_id) or self._read_persisted(batch_id)

    def list_batches(self) -> list[dict[str, Any]]:
        # 内存里有的用最新对象；重启后落库的底稿也要能看到。
        for name in os.listdir(DATA_DIR):
            if not (name.startswith("cost_batch_") and name.endswith(".json")):
                continue
            batch_id = name[len("cost_batch_"):-len(".json")]
            if batch_id in self._batches:
                continue
            batch = self._read_persisted(batch_id)
            if batch is not None:
                self._batches[batch_id] = batch
        return sorted(
            self._batches.values(),
            key=lambda item: item["batch_id"],
            reverse=True,
        )

    # ---------- 底稿结构 ----------
    def _asset_sheet(
        self,
        entry: dict[str, Any],
        baseline: dict[str, Any],
        policy: str,
        decisions: dict[str, str],
    ) -> dict[str, Any]:
        asset_id = str(entry.get("关联资产", ""))
        asset = {
            "asset_id": asset_id,
            "entry_id": int(entry.get("id", 0)),
            "record_id": str(entry.get("记录编号", "")),
            "baseline": baseline,
            "current": baseline,
            "latest_sampling_point": baseline["sampling_point"],
            "baseline_total": baseline["total"],
            "current_total": baseline["total"],
            "variance_total": 0.0,
            "included_amount": 0.0,
            "carried_amount": 0.0,
            "confirmed_at": str(entry.get("确认时间") or ""),
            "confirmed": str(entry.get("status")) == "已确认",
            "decision": decisions.get(asset_id, policy),
            "decision_reason": "开批采样",
            "items": [],
            "applied_total": 0.0,
        }
        self._refresh_asset(asset, baseline, decisions, policy)
        return asset

    def _refresh_asset(
        self,
        asset: dict[str, Any],
        current: dict[str, Any],
        decisions: dict[str, str],
        policy: str,
    ) -> None:
        baseline = asset["baseline"]
        items: list[dict[str, Any]] = []
        variance_total = 0.0
        for key in ("storage", "compute", "transfer"):
            before = baseline["items"][key]
            after = current["items"][key]
            variance = round(after["amount"] - before["amount"], 2)
            variance_total += variance
            item = {
                "item": key,
                "label": after["label"],
                "source_module": after["source_module"],
                "job_id": after["job_id"],
                "job_no": after["job_no"],
                "baseline_usage": before["usage_text"],
                "current_usage": after["usage_text"],
                "baseline_usage_gb": before["usage_gb"],
                "current_usage_gb": after["usage_gb"],
                "baseline_amount": before["amount"],
                "current_amount": after["amount"],
                "amount_text": after["amount_text"],
                "variance": variance,
                "variance_text": money(variance),
                "changed": abs(variance) >= 0.005 or before["usage"] != after["usage"],
                "revision": int(after.get("revision") or 0),
                "measurement_kind": after.get("measurement_kind") or after.get("kind"),
                "usage_basis": after["basis"],
                "usage_confirmed_at": after.get("confirmed_at", ""),
                "sampled_at": current["sampled_at"],
                "sampling_point": current["sampling_point"],
                "rate": after["rate"],
                "applied_amount": 0.0,
                "pending_delta": variance,
            }
            items.append(item)

        asset["current"] = current
        asset["items"] = items
        asset["current_total"] = current["total"]
        asset["variance_total"] = round(variance_total, 2)

        # 逐资产指定优先，其次使用批次口径；每轮重算都重新判定，避免上一轮
        # 的临时选择被悄悄沿用。
        requested = decisions.get(asset["asset_id"]) or policy
        if requested not in (DECISION_NOW, DECISION_NEXT):
            requested = policy

        effective = requested
        if asset.get("confirmed_at"):
            # 已确认资产：确认时点之后才落库的修订只能结转下一批；
            # 确认前就已采用的用量可以补记到本批，保证“只计一次”。
            post_confirm = [
                item for item in items
                if item["usage_confirmed_at"]
                and item["usage_confirmed_at"] > asset["confirmed_at"]
                and abs(item["current_amount"] - item["baseline_amount"]) >= 0.005
            ]
            if post_confirm:
                effective = DECISION_NEXT
                asset["decision_reason"] = (
                    f"该资产已于 {asset['confirmed_at']} 确认，"
                    f"确认后修订（{post_confirm[0]['label']}）结转下一批"
                )
            else:
                effective = DECISION_NOW
                asset["decision_reason"] = (
                    f"该资产已于 {asset['confirmed_at']} 确认，无确认后修订"
                )
        else:
            asset["decision_reason"] = (
                "按批次口径差额本次入账"
                if effective == DECISION_NOW
                else "按批次口径差额结转下一批"
            )

        asset["requested_decision"] = requested
        asset["decision"] = effective
        asset["effective_decision"] = effective
        asset["pending_delta"] = round(variance_total, 2)
    # ---------- 入账 ----------
    def _apply_accounting(self, batch: dict[str, Any], *, stage: str) -> None:
        """把“应计 - 已入账”的差额写进台账；重复执行差额为 0，不会二次入账。"""
        ledger_before = len(batch["ledger"])
        applied: dict[tuple[str, str], float] = {}
        for row in batch["ledger"]:
            key = (str(row.get("asset_id")), str(row.get("item")))
            applied[key] = round(applied.get(key, 0.0) + float(row.get("amount") or 0.0), 2)

        for asset in batch["assets"]:
            # 应计口径：本次入账按最新采样，结转下一批按开批基线；
            # 已确认资产在此基础上由 _refresh_asset 决定是否冻结。
            effective = asset.get("effective_decision") or asset["decision"]
            for item in asset["items"]:
                key = (asset["asset_id"], item["item"])
                already = round(applied.get(key, 0.0), 2)
                target = (
                    round(item["current_amount"], 2)
                    if effective == DECISION_NOW
                    else round(item["baseline_amount"], 2)
                )
                delta = round(target - already, 2)
                item["applied_amount"] = already
                item["pending_delta"] = delta
                if abs(delta) < 0.005:
                    continue
                ledger_row = {
                    "ledger_id": f"{batch['batch_id']}-L{len(batch['ledger']) + 1:04d}",
                    "batch_id": batch["batch_id"],
                    "asset_id": asset["asset_id"],
                    "item": item["item"],
                    "label": item["label"],
                    "revision": item["revision"],
                    "usage": item["current_usage"],
                    "usage_gb": item["current_usage_gb"],
                    "amount": delta,
                    "amount_text": money(delta),
                    "account_after": round(already + delta, 2),
                    "basis": item["usage_basis"],
                    "sampling_point": item["sampling_point"],
                    "usage_confirmed_at": item["usage_confirmed_at"],
                    "decision": effective,
                    "decision_reason": asset["decision_reason"],
                    "stage": stage,
                    "applied_at": _now(),
                }
                batch["ledger"].append(ledger_row)
                applied[key] = round(already + delta, 2)

            for item in asset["items"]:
                key = (asset["asset_id"], item["item"])
                booked = round(applied.get(key, 0.0), 2)
                item["applied_amount"] = booked
                item["pending_delta"] = round(item["current_amount"] - booked, 2)
                item["accounted"] = booked

            # 已入账金额来自台账；未入账的差额就是结转下一批的部分。
            asset["applied_total"] = round(
                sum(
                    float(row["amount"])
                    for row in batch["ledger"]
                    if row["asset_id"] == asset["asset_id"]
                ),
                2,
            )
            asset["included_amount"] = asset["applied_total"]
            asset["carried_amount"] = round(
                sum(item["pending_delta"] for item in asset["items"]),
                2,
            )

        baseline_total = round(sum(a["baseline_total"] for a in batch["assets"]), 2)
        current_total = round(sum(a["current_total"] for a in batch["assets"]), 2)
        variance_total = round(sum(a["variance_total"] for a in batch["assets"]), 2)
        included_total = round(sum(a["included_amount"] for a in batch["assets"]), 2)
        carried_total = round(sum(a["carried_amount"] for a in batch["assets"]), 2)
        batch_total = round(sum(float(row["amount"]) for row in batch["ledger"]), 2)

        confirmation_times = [
            {
                "asset_id": asset["asset_id"],
                "confirmed_at": asset["confirmed_at"] or "",
                "current_revision": max(
                    (int(item["revision"]) for item in asset["items"]),
                    default=0,
                ),
                "amount": asset["applied_total"],
            }
            for asset in batch["assets"]
        ]
        batch["confirmation_times"] = confirmation_times
        new_rows = batch["ledger"][ledger_before:]
        batch["last_applied"] = {
            "rows": len(new_rows),
            "amount": round(sum(float(row["amount"]) for row in new_rows), 2),
            "amount_text": money(sum(float(row["amount"]) for row in new_rows)),
        }
        batch["totals"] = {
            "baseline": baseline_total,
            "current": current_total,
            "variance": variance_total,
            "included": included_total,
            "carried": carried_total,
            "batch_total": batch_total,
            "asset_count": len(batch["assets"]),
            "ledger_count": len(batch["ledger"]),
            "baseline_text": money(baseline_total),
            "current_text": money(current_total),
            "variance_text": money(variance_total),
            "included_text": money(included_total),
            "carried_text": money(carried_total),
            "batch_total_text": money(batch_total),
            "last_applied": batch.get("last_applied", {}).get("amount", 0.0),
            "last_applied_text": batch.get("last_applied", {}).get("amount_text", "0.00"),
        }
        batch["status"] = "已确认" if all(a["confirmed_at"] for a in batch["assets"]) else "打开"

    # ---------- 辅助 ----------
    def _confirm(self, batch: dict[str, Any], entry_ids: list[int]) -> None:
        wanted = {int(value) for value in entry_ids}
        stamp = _now()
        for asset in batch["assets"]:
            if int(asset["entry_id"]) in wanted and not asset.get("confirmed_at"):
                asset["confirmed_at"] = stamp
                asset["confirmed"] = True
                entry = store.find("cost_tracker", int(asset["entry_id"]))
                if entry is not None:
                    entry["status"] = "已确认"
                    entry["pending"] = False
                    entry["确认时间"] = stamp
                    entry["入账批次"] = batch["batch_id"]
                    entry["已入账金额"] = money(asset["applied_total"])

    def _resolve_entries(self, entry_ids: list[int]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in entry_ids or []:
            try:
                entry_id = int(raw)
            except (TypeError, ValueError):
                continue
            if entry_id in seen:
                continue
            entry = store.find("cost_tracker", entry_id)
            if entry is not None:
                seen.add(entry_id)
                entries.append(entry)
        return entries

    def _build_index(self, asset_ids: list[str]) -> BatchIndex:
        # 补数任务对外提供「已确认的计量数据」，批量底稿只消费它们的修订版本。
        from app.services import service_for

        backfill = service_for("backfill_job")
        measurements = backfill.confirmed_measurements(asset_ids) if backfill else []
        return BatchIndex(asset_ids, measurements)

    def _next_batch_id(self) -> str:
        existing = set(self._batches)
        for name in os.listdir(DATA_DIR):
            if name.startswith("cost_batch_") and name.endswith(".json"):
                existing.add(name[len("cost_batch_"):-len(".json")])
        seq = 1
        while True:
            candidate = f"{self.PREFIX}-{seq:04d}"
            if candidate not in existing:
                return candidate
            seq += 1

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"批量核算底稿 {batch_id} 不存在")
        self._batches[batch_id] = batch
        return batch

    def _persist(self, batch: dict[str, Any]) -> None:
        path = os.path.join(DATA_DIR, f"cost_batch_{batch['batch_id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False, indent=2, default=str)

    def _read_persisted(self, batch_id: str) -> dict[str, Any] | None:
        path = os.path.join(DATA_DIR, f"cost_batch_{batch_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


_shared_batches = CostBatchService()


def batch_service() -> CostBatchService:
    return _shared_batches
