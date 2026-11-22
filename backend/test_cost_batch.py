"""批量核算底稿回归测试：A/B 跨口径差额、修订版本与幂等入账。

场景（与需求一致）：
    1. A、B 一起开批，开批时 B 的补数为预估 20GB；
    2. 逐条核算与批量核算对比源作业读取情况；
    3. A 确认后 B 补数完工，实测 40GB（修订 2）；
    4. 重算：B 的版本与整组合计可核对，差额本次入账；
    5. 重新打开底稿并再重算两次：同一笔账只增加一次 20GB 对应的增量。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from app.services import service_for
from app.services import backfill_job as backfill_module
from app.services import cost_batch as cost_batch_module
from app.services import cost_tracker as cost_tracker_module
from app.store import store

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0
SANDBOX = tempfile.mkdtemp(prefix="cost-batch-test-")
# 把数据产物写到临时目录，避免测试污染仓库内的示例数据。
cost_batch_module.DATA_DIR = SANDBOX
cost_tracker_module.DATA_DIR = SANDBOX
backfill_module.DATA_DIR = SANDBOX
SHARED: dict[str, object] = {}


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
        return False
    return True


def asset_sheet(batch, asset_id):
    return next(asset for asset in batch["assets"] if asset["asset_id"] == asset_id)


def compute_item(asset):
    return next(item for item in asset["items"] if item["item"] == "compute")


def ledger_rows(batch, asset_id, item="compute"):
    return [
        row for row in batch["ledger"]
        if row["asset_id"] == asset_id and row["item"] == item
    ]


def backfill_job_for(asset_id):
    return next(row for row in store.rows("backfill_job") if row["目标资产"] == asset_id)


def test_batch_open_samples_once():
    print("\n=== 开批：多选资产一次采样，三项共享位置 ===")
    service = service_for("cost_tracker")
    batch = service.open_batch([4, 5], label="A/B 对照批")
    SHARED["batch_id"] = batch["batch_id"]
    check("底稿生成", batch["batch_id"].startswith("BATCH-"))
    check("底稿含两条资产", len(batch["assets"]) == 2)

    asset_a = asset_sheet(batch, "ASSET-A")
    asset_b = asset_sheet(batch, "ASSET-B")
    check("A 与 B 共享同一采样位置",
          asset_a["baseline"]["sampling_point"] == asset_b["baseline"]["sampling_point"])
    check("同一资产三项共享一次采样位置",
          len({item["sampling_point"] for item in asset_b["items"]}) == 1)
    check("批量只做一次源作业读取", batch["read_stats"]["backfill_measurement_calls"] == 1,
          batch.get("read_stats"))
    print(f"    读取统计: {batch['read_stats']}")

    item_b = compute_item(asset_b)
    check("开批判定 B 为预估 20GB",
          item_b["baseline_usage"] == "20GB" and item_b["measurement_kind"] == "预估")
    check("开批时 B 计算成本 1.00", item_b["baseline_amount"] == 1.0)
    check("整组合计 = 12.90 + 3.80 = 16.70",
          abs(batch["totals"]["current"] - 16.7) < 1e-6, batch["totals"])
    check("同一资产的三个计费项齐备",
          [item["item"] for item in asset_b["items"]] == ["storage", "compute", "transfer"])


def test_single_vs_batch_reads():
    print("\n=== 逐条核算 vs 批量核算的作业读取情况 ===")
    service = service_for("cost_tracker")
    single_a, _ = service.calculate_cost(4)
    single_b, _ = service.calculate_cost(5)
    check("逐条核算 A 记录采样位置", bool(single_a.get("核算采样位置")))
    check("逐条核算 B 记录实际采用用量",
          single_b["实际采用用量"]["compute"]["usage"] == "20GB")
    check("逐条核算 B 采样位置互不相同",
          single_a["核算采样位置"] != single_b["核算采样位置"])
    check("逐条核算各自读取一次补数作业",
          single_a["核算采样位置"] and single_b["核算采样位置"])

    batch = service.get_batch(str(SHARED["batch_id"]))
    # 逐条：两条核算各自构建一次索引 → 2 次补数读取、各自全量扫描。
    # 批量：整批只构建一次索引 → 1 次补数读取，资产数增加不再增加读取。
    check("批量读取次数 = 1，低于逐条的 2",
          batch["read_stats"]["backfill_measurement_calls"] == 1)
    check("批量采样位置统一",
          len({asset["baseline"]["sampling_point"] for asset in batch["assets"]}) == 1)


def test_backfill_measurement_revisions():
    print("\n=== 补数完工保存实测用量与修订次序 ===")
    service = service_for("backfill_job")
    job = backfill_job_for("ASSET-B")

    entry, message = service.complete_backfill(job["id"], "40GB")
    check("补数完工成功", entry is not None, message)
    revisions = entry.get("计量修订") or []
    check("修订次序连续（预估 1 → 实测 2）",
          [r["revision"] for r in revisions] == [1, 2], revisions)
    check("修订 1 是预估 20GB",
          revisions[0]["usage"] == "20GB" and revisions[0]["kind"] == "预估")
    check("修订 2 是实测 40GB",
          revisions[1]["usage"] == "40GB" and revisions[1]["kind"] == "实测")
    check("实测用量落库", entry.get("实测用量") == "40GB")
    check("当前计量版本指向修订 2", entry.get("当前计量版本") == 2)
    check("已完工但重复确认不新增修订",
          service.complete_backfill(job["id"], "40GB")[0]["当前计量版本"] == 2)

    measurement = service.latest_measurement("ASSET-B")
    check("补数对外提供已确认计量",
          measurement is not None and measurement["revision"] == 2, measurement)
    check("计量关联到实际作业",
          measurement is not None and measurement["job_id"] == job["id"])
    check("计量带确认时间", bool(measurement and measurement["confirmed_at"]))


def test_recalc_after_revision():
    print("\n=== A 确认后提交 B 修订，重算核对版本与整组合计 ===")
    service = service_for("cost_tracker")
    batch_id = str(SHARED["batch_id"])
    service.confirm_batch(batch_id, [4])

    batch = service.recalculate_batch(batch_id)
    asset_b = asset_sheet(batch, "ASSET-B")
    item_b = compute_item(asset_b)
    check("本轮 B 采用修订 2", item_b["revision"] == 2)
    check("本轮 B 采用实测 40GB",
          item_b["current_usage"] == "40GB" and item_b["measurement_kind"] == "实测")
    check("B 计算差额 = 40GB*0.05 - 20GB*0.05 = 1.00",
          abs(item_b["variance"] - 1.0) < 1e-6, item_b["variance"])
    check("整组合计 = 旧 16.70 + 差额 1.00 = 17.70",
          abs(batch["totals"]["current"] - 17.7) < 1e-6, batch["totals"])
    check("整组合计只取一次差额",
          abs(batch["totals"]["included"] - 17.7) < 1e-6)
    check("A 已确认、无确认后修订，保持本次入账",
          asset_sheet(batch, "ASSET-A")["decision"] == "本次入账")
    asset_a = asset_sheet(batch, "ASSET-A")
    check("合计能解释各项数据的确认时间",
          bool(asset_a["confirmed_at"])
          and all(item["usage_confirmed_at"] or item["usage_basis"]
                  for item in asset_a["items"]))
    check("底稿保留每笔实际采用用量",
          all(item["current_usage"] or item["usage_basis"] for item in asset_b["items"]))
    check("B 差额附核算依据落库",
          all(row["basis"] and row["decision_reason"] for row in ledger_rows(batch, "ASSET-B")))


def test_recalc_twice_is_idempotent():
    print("\n=== 重新打开底稿并重算两次，只计一次 20GB 增量 ===")
    service = service_for("cost_tracker")
    batch_id = str(SHARED["batch_id"])
    # 重新打开：从落库文件恢复，模拟换人复核。
    reopened = service.get_batch(batch_id)
    check("底稿可重新打开", reopened is not None and reopened["batch_id"] == batch_id)

    first = service.recalculate_batch(batch_id)
    second = service.recalculate_batch(batch_id)

    for label, batch in (("第一次", first), ("第二次", second)):
        total_b_compute = sum(row["amount"] for row in ledger_rows(batch, "ASSET-B"))
        check(f"{label}重算后 B 计算项累计 = 2.00（1.00 基线 + 1.00 差额）",
              abs(total_b_compute - 2.0) < 1e-6, total_b_compute)
        check(f"{label}重算后整组合计仍为 17.70",
              abs(batch["totals"]["current"] - 17.7) < 1e-6, batch["totals"])
        check(f"{label}重算后已入账合计 17.70",
              abs(batch["totals"]["included"] - 17.7) < 1e-6, batch["totals"])

    check("两次重算没有再新增台账行",
          len(first["ledger"]) == len(second["ledger"]), (
              len(first["ledger"]), len(second["ledger"])))
    check("第二次重算本次新增入账为 0",
          abs(second["totals"]["last_applied"]) < 1e-6, second["totals"]["last_applied_text"])
    check("B 只产生基线 + 一条差额入账",
          len(ledger_rows(second, "ASSET-B")) == 2,
          ledger_rows(second, "ASSET-B"))

    persisted = os.path.join(SANDBOX, f"cost_batch_{batch_id}.json")
    check("底稿已落库", os.path.exists(persisted))


def test_carried_variance_for_late_revision():
    print("\n=== 确认后才落库的修订：差额结转下一批 ===")
    service = service_for("cost_tracker")
    batch = service.open_batch([5], label="结转批")
    batch_id = batch["batch_id"]
    service.confirm_batch(batch_id, [5])

    # 确认之后补数再次修订为 60GB；已确认的 40GB 不能被改写。
    job = backfill_job_for("ASSET-B")
    service_for("backfill_job").submit_revision(job["id"], "60GB", source="完工复测")

    batch = service.recalculate_batch(batch_id)
    asset = asset_sheet(batch, "ASSET-B")
    item = compute_item(asset)
    check("确认后修订结转下一批", asset["decision"] == "结转下一批", asset["decision"])
    check("差额去向有理由说明", "结转" in asset["decision_reason"], asset["decision_reason"])
    check("已确认金额不被改写",
          abs(item["applied_amount"] - 2.0) < 1e-6, item["applied_amount"])
    check("结转差额 = 60GB*0.05 - 40GB*0.05 = 1.00",
          abs(asset["carried_amount"] - 1.0) < 1e-6, asset["carried_amount"])
    check("底稿同时保留最新修订与确认时点",
          item["revision"] == 3 and bool(asset["confirmed_at"]))


if __name__ == "__main__":
    print("=" * 60)
    print("批量核算底稿回归测试")
    print("=" * 60)
    try:
        test_batch_open_samples_once()
        test_single_vs_batch_reads()
        test_backfill_measurement_revisions()
        test_recalc_after_revision()
        test_recalc_twice_is_idempotent()
        test_carried_variance_for_late_revision()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)
    if failed:
        sys.exit(1)
