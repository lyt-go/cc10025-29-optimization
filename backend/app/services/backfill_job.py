"""backfill_job 领域服务：补数任务管理，接收 partition_plan 上游事件。

跨模块数据流：
    partition_plan → backfill_job: 分区方案生效触发补数
    backfill_job → cost_tracker: 补数完工提供已确认计量数据，供批量核算底稿采样

计量口径：
    - 派单时把「数据量」固化为修订 1（预估）；
    - 确认完工时追加一条实测修订（修订次序 +1），保存本次实测用量；
    - 完工后仍可提交人工修订，但只有用量真正变化才追加，避免重复入账。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.measurement import format_gb, latest_revision, parse_gb
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "补数作业"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["作业编号", "目标资产", "补数区间", "数据量", "实测用量", "当前计量版本", "依赖检查", "作业状态"]
    REQUIRED_FIELDS = ["作业编号", "目标资产", "补数区间"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "已失败"]
    ACTION_RULES = {"发起执行": "执行中", "确认完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 生命周期 ----------
    def start_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        interval = str(entry.get("补数区间", ""))

        entry["status"] = "执行中"
        entry["pending"] = True
        entry["作业状态"] = "执行中"

        # 派单即固化预估口径：没有修订历史时补一条修订 1。
        if not entry.get("计量修订"):
            self._append_revision(
                entry,
                usage=str(entry.get("数据量") or ""),
                kind="预估",
                source="补数派单",
                force=True,
            )
            entry["预估用量"] = str(entry.get("数据量") or "")
        entry["计量类型"] = "预估"

        self._write_backfill_record(asset_id, entry)
        self._update_cost_for_backfill(asset_id)

        return entry, f"{self.ENTITY} {entry_id} 执行中，{interval}"

    def complete_backfill(
        self,
        entry_id: int,
        actual_usage: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """确认完工：保存本次实测用量及修订次序，供批量核算比较。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("目标资产", ""))
        usage = (
            actual_usage
            or str(entry.get("实测用量") or "")
            or str(entry.get("实际用量") or "")
            or str(entry.get("数据量") or "")
        )

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["作业状态"] = "已完成"

        # 若作业从未派单过，先把派单口径的预估用量固化为修订 1，
        # 这样完工的实测用量自然成为修订 2，底稿能对照“原估/实测”。
        if not entry.get("计量修订"):
            estimate = str(
                entry.get("预估用量") or entry.get("数据量") or ""
            ).strip()
            if estimate and parse_gb(estimate) != parse_gb(usage):
                self._append_revision(
                    entry, usage=estimate, kind="预估", source="补数派单")
                entry["预估用量"] = estimate

        # 再次确认同一用量时保持幂等，不制造第二条相同修订。
        revision = self._append_revision(
            entry,
            usage=usage,
            kind="实测",
            source="补数完工",
        )
        entry["预估用量"] = entry.get("预估用量") or self._estimate_usage(entry)
        entry["实测用量"] = str(revision.get("usage") or usage)
        entry["数据量"] = str(revision.get("usage") or usage)
        entry["计量类型"] = "实测"
        entry["计量确认时间"] = revision.get("confirmed_at") or _now()
        entry["当前计量版本"] = int(revision.get("revision") or 1)

        self._write_backfill_record(asset_id, entry)
        self._update_cost_for_backfill(asset_id)

        return entry, (
            f"{self.ENTITY} {entry_id} 已完成，实测用量 {entry.get('实测用量')}，"
            f"计量修订次序 {entry['当前计量版本']}"
        )

    def submit_revision(
        self,
        entry_id: int,
        usage: str | None = None,
        kind: str = "实测",
        source: str = "人工修订",
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """提交用量修订：只有用量变化才生成新修订，避免重复计量。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        usage = str(usage or entry.get("实测用量") or entry.get("数据量") or "")
        latest = self._latest_revision(entry)
        if latest and str(latest.get("usage")) == usage and latest.get("kind") == kind:
            return entry, f"{self.ENTITY} {entry_id} 用量未变化，沿用修订 {latest.get('revision')}"

        revision = self._append_revision(
            entry, usage=usage, kind=kind, source=source, note=note)
        entry["实测用量"] = str(revision.get("usage") or usage)
        entry["数据量"] = str(revision.get("usage") or usage)
        entry["计量类型"] = kind
        entry["计量确认时间"] = revision.get("confirmed_at")
        entry["当前计量版本"] = int(revision.get("revision") or 1)

        asset_id = str(entry.get("目标资产", ""))
        self._write_backfill_record(asset_id, entry)
        self._update_cost_for_backfill(asset_id)
        return entry, (
            f"{self.ENTITY} {entry_id} 已提交修订 {entry['当前计量版本']}，"
            f"用量 {entry['实测用量']}"
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        values = self._action_values
        if action == "发起执行":
            return self.start_backfill(entry_id)
        if action == "确认完成":
            usage = values.get("实测用量") or values.get("实际用量") or values.get("usage")
            return self.complete_backfill(entry_id, str(usage) if usage else None)
        if action == "修订用量":
            usage = values.get("实测用量") or values.get("实际用量") or values.get("usage")
            kind = str(values.get("计量类型") or "实测")
            source = str(values.get("来源") or "人工修订")
            note = str(values.get("说明") or values.get("remark") or "")
            return self.submit_revision(entry_id, str(usage) if usage else None,
                                        kind=kind, source=source, note=note)
        return super().run_action(entry_id, action)

    # ---------- 对批量核算提供的计量接口 ----------
    def latest_measurement(self, asset_id: str) -> dict[str, Any] | None:
        """返回某资产当前生效的补数计量（关联到具体作业）。"""
        candidates = [
            row for row in store.rows(self.MODULE)
            if str(row.get("目标资产", "")) == asset_id and self._latest_revision(row)
        ]
        if not candidates:
            return None

        def _key(item: dict[str, Any]) -> tuple[int, int]:
            revision = self._latest_revision(item) or {}
            return (int(revision.get("revision") or 0), int(item.get("id", 0)))

        row = max(candidates, key=_key)
        revision = self._latest_revision(row)
        assert revision is not None
        return self._measurement_payload(row, revision)

    def confirmed_measurements(
        self, asset_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """补数任务对外提供已确认的计量数据，供批量核算底稿使用。

        每条修订在写入时即带确认时间，取该作业当前生效的修订；`kind`
        区分“预估 / 实测”，底稿据此对照开批口径与最新口径。
        """
        wanted = set(asset_ids or [])
        results: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            asset_id = str(row.get("目标资产", ""))
            if wanted and asset_id not in wanted:
                continue
            revision = self._latest_revision(row)
            if revision is None:
                continue
            if not str(revision.get("usage") or "").strip():
                continue
            results.append(self._measurement_payload(row, revision))
        return results

    def measurement_history(self, asset_id: str) -> list[dict[str, Any]]:
        """返回某资产全部修订，按修订次序升序，供底稿对照。"""
        results: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if str(row.get("目标资产", "")) != asset_id:
                continue
            for revision in row.get("计量修订") or []:
                if isinstance(revision, dict):
                    results.append(self._measurement_payload(row, revision))
            if not row.get("计量修订"):
                revision = self._latest_revision(row)
                if revision:
                    results.append(self._measurement_payload(row, revision))
        return sorted(results, key=lambda item: (item["revision"], item["job_id"]))

    # ---------- 修订维护 ----------
    def _estimate_usage(self, entry: dict[str, Any]) -> str:
        revisions = entry.get("计量修订") or []
        if revisions:
            return str(revisions[0].get("usage") or "")
        return str(entry.get("数据量") or entry.get("预估用量") or "")

    def _latest_revision(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        return latest_revision(entry)

    def _append_revision(
        self,
        entry: dict[str, Any],
        *,
        usage: str,
        kind: str,
        source: str,
        note: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        revisions = [
            revision for revision in (entry.get("计量修订") or [])
            if isinstance(revision, dict)
        ]
        latest = max(revisions, key=lambda item: int(item.get("revision", 0))) if revisions else None
        if latest and not force and str(latest.get("usage")) == usage and latest.get("kind") == kind:
            return latest

        revision_no = int(latest.get("revision", 0) if latest else 0) + 1
        usage_gb = parse_gb(usage)
        confirmed_at = _now()
        revision = {
            "revision": revision_no,
            "修订次序": revision_no,
            "usage": usage,
            "用量": usage,
            "usage_gb": usage_gb,
            "用量GB": usage_gb,
            "kind": kind,
            "计量类型": kind,
            "source": source,
            "来源": source,
            "note": note,
            "confirmed_at": confirmed_at,
            "确认时间": confirmed_at,
        }
        revisions.append(revision)
        revisions.sort(key=lambda item: int(item.get("revision", 0)))
        entry["计量修订"] = revisions
        entry["当前计量版本"] = revision_no
        return revision

    def _measurement_payload(
        self, entry: dict[str, Any], revision: dict[str, Any],
    ) -> dict[str, Any]:
        usage = str(revision.get("usage") or "")
        usage_gb = revision.get("usage_gb")
        if usage_gb is None:
            usage_gb = parse_gb(usage)
        return {
            "asset_id": str(entry.get("目标资产", "")),
            "job_module": self.MODULE,
            "job_id": int(entry.get("id", 0)),
            "job_no": str(entry.get("作业编号", "")),
            "revision": int(revision.get("revision") or 1),
            "修订次序": int(revision.get("revision") or 1),
            "usage": usage,
            "用量": usage,
            "usage_gb": usage_gb,
            "用量GB": usage_gb,
            "usage_text": usage or format_gb(usage_gb),
            "kind": str(revision.get("kind") or "预估"),
            "计量类型": str(revision.get("kind") or "预估"),
            "confirmed_at": str(revision.get("confirmed_at") or ""),
            "确认时间": str(revision.get("confirmed_at") or ""),
            "source": str(revision.get("source") or ""),
            "status": str(entry.get("status") or ""),
        }

    # ---------- 文件 I/O / 跨模块副作用 ----------
    def _write_backfill_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        job_id = str(entry.get("作业编号", ""))
        filename = f"backfill_{job_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        revisions = entry.get("计量修订") or []
        latest = self._latest_revision(entry) or {}
        record = {
            "job_id": job_id,
            "asset_id": asset_id,
            "interval": str(entry.get("补数区间", "")),
            "estimated_usage": str(entry.get("预估用量") or ""),
            "actual_usage": str(entry.get("实测用量") or latest.get("usage") or ""),
            "current_revision": int(entry.get("当前计量版本") or 0),
            "revision_order": [int(r.get("revision", 0)) for r in revisions],
            "revisions": revisions,
            "measurement_kind": str(entry.get("计量类型", "")),
            "measurement_confirmed_at": str(entry.get("计量确认时间") or ""),
            "data_volume": str(entry.get("数据量", "")),
            "dependency_check": str(entry.get("依赖检查", "")),
            "priority": str(entry.get("优先级", "")),
            "retry_count": str(entry.get("重试次数", "")),
            "status": str(entry.get("作业状态", "")),
            "updated_at": _now(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_cost_for_backfill(self, asset_id: str) -> None:
        """把补数计量挂到成本记录上，供底稿/页面直接看到口径与版本。

        这里不覆盖已核算金额：金额只在核算（单条或批量）采样时统一回写。
        """
        measurement = self.latest_measurement(asset_id) or {}
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                row["补数作业"] = str(measurement.get("job_no") or "")
                row["补数计量用量"] = str(measurement.get("usage") or "")
                row["补数计量版本"] = int(measurement.get("revision") or 0)
                row["补数计量类型"] = str(measurement.get("kind") or "")
                row["pending"] = True
                break
