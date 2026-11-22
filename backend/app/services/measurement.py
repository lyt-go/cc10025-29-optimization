"""计量口径工具：把冷存、补数、回迁作业里的用量统一换算成可复算的 GB 口径。

批量核算底稿要求“同一资产的三个计费项按一次采样位置复算”，因此这里只做
纯函数换算，不读 store；采样由 CostTrackerService 统一完成，避免逐项反复扫描。
"""
from __future__ import annotations

import re
from typing import Any

# 三个计费项：存储、计算、传输。key 同时用于底稿和计量台账。
ITEM_LABELS = {
    "storage": "存储成本",
    "compute": "计算成本",
    "transfer": "传输成本",
}
ITEM_RATES = {
    "storage": 0.02,
    "compute": 0.05,
    "transfer": 0.09,
}
# 源作业缺失时沿用旧成本报告的兜底值，保证历史记录可解释。
DEFAULT_AMOUNTS = {
    "storage": 5.0,
    "compute": 10.0,
    "transfer": 3.0,
}
# 源作业存在但用量仍是“待估算”时，沿用旧逐条核算的兜底金额。
UNMEASURED_AMOUNTS = {
    "storage": 10.0,
    "compute": 20.0,
    "transfer": 15.0,
}
ITEM_SOURCE_MODULES = {
    "storage": "cold_storage",
    "compute": "backfill_job",
    "transfer": "hot_prefetch",
}
ITEM_SOURCE_FIELDS = {
    "storage": "迁移量",
    "compute": "数据量",
    "transfer": "回迁量",
}

_UNIT_FACTORS = {
    "TB": 1024.0,
    "GB": 1.0,
    "MB": 1.0 / 1024.0,
    "KB": 1.0 / (1024.0 * 1024.0),
    "B": 1.0 / (1024.0 * 1024.0 * 1024.0),
}
_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)?", re.IGNORECASE)


def parse_gb(raw: Any) -> float | None:
    """把 '20GB' / '1.5TB' / '512MB' 之类的用量换算为 GB。

    返回 None 表示这条数据没有可用用量（例如“待估算”“补数任务样例1”），
    调用方应使用兜底金额而不是把它当成 0。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in {"-", "—", "待估算", "未评估", "待确认"}:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "GB").upper()
    return round(value * _UNIT_FACTORS.get(unit, 1.0), 6)


def format_gb(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))}GB"
    return f"{value:.3f}".rstrip("0").rstrip(".") + "GB"


def amount_for(item: str, usage_gb: float | None, *, measured: bool = False) -> float:
    if usage_gb is None:
        return float(UNMEASURED_AMOUNTS[item] if measured else DEFAULT_AMOUNTS[item])
    return round(usage_gb * ITEM_RATES[item], 2)


def money(value: float | None) -> str:
    return f"{float(value or 0.0):.2f}"


def derived_kind(status: Any, explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    return "实测" if str(status or "") in {"已完成", "已确认"} else "预估"


def latest_revision(row: dict[str, Any]) -> dict[str, Any] | None:
    """取出补数作业当前生效的计量修订。

    优先使用显式的 `计量修订` 列表；老数据没有修订列表时，从
    `实测用量` / `数据量` 推导一个只读版本，避免升级后历史行不可核算。
    """
    revisions = row.get("计量修订") or []
    if isinstance(revisions, list) and revisions:
        valid = [r for r in revisions if isinstance(r, dict)]
        if valid:
            return max(valid, key=lambda item: int(item.get("revision", 0)))
    usage = row.get("实测用量") or row.get("实际用量") or row.get("数据量")
    if not str(usage or "").strip():
        return None
    revision = int(row.get("当前计量版本") or 1)
    return {
        "revision": revision,
        "usage": str(usage),
        "kind": derived_kind(row.get("作业状态") or row.get("status"),
                             row.get("计量类型")),
        "confirmed_at": str(row.get("计量确认时间") or row.get("确认时间") or ""),
        "source": str(row.get("计量来源") or "补数作业"),
    }
