"""药房与取药流程测试。

覆盖三件事：
  1. 状态机：只有走完缴费才能选取药时间，不能跳步
  2. 库存：缴费那一刻才扣，且并发下绝不超发（和号源是同一类问题）
  3. 金额：用整数分算，不用浮点（对账时不会出现几分钱误差）

跑法：
    .venv\\Scripts\\python.exe -m pytest tests/test_pharmacy.py -v -s
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import Base, enable_sqlite_pragmas
from app.domain.models import (
    Appointment,
    Department,
    Doctor,
    Medicine,
    Patient,
    Prescription,
    Schedule,
)
from app.services import pharmacy

WORKERS = 8
BUSY_TIMEOUT_MS = 5000


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'pharmacy.db'}", future=True, poolclass=NullPool)

    @event.listens_for(eng, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        enable_sqlite_pragmas(dbapi_conn)
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def sample(engine):
    """一位患者 + 一位医生 + 两种药。"""
    with Session(engine) as db:
        dep = Department(name="内科", keywords="测试", description="测试")
        db.add(dep)
        db.flush()
        doc = Doctor(name="测试医生", title="主治医师", department_id=dep.id)
        db.add(doc)
        db.flush()
        patient = Patient(name="测试患者", phone="13900001111", id_card="460100200401019999")
        db.add(patient)
        db.flush()

        a = Medicine(name="药A", spec="10片", unit="盒", price_cents=1234, stock=10, warn_stock=3)
        b = Medicine(name="药B", spec="20片", unit="盒", price_cents=56, stock=5, warn_stock=2)
        db.add_all([a, b])
        db.commit()
        return {"patient_id": patient.id, "doctor_id": doc.id,
                "med_a": a.id, "med_b": b.id}


def _make_prescription(engine, sample, items) -> int:
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=sample["patient_id"], doctor_id=sample["doctor_id"], items=items)
        return rx.id


def _stock(engine, medicine_id) -> int:
    with Session(engine) as db:
        return db.get(Medicine, medicine_id).stock


# --------------------------------------------------------------------------
# 1. 状态机
# --------------------------------------------------------------------------
def test_new_prescription_is_pending_pay(engine, sample):
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 2}])
    with Session(engine) as db:
        rx = pharmacy.get(db, rx_id)
        assert rx.status == "pending_pay"
        assert rx.code.startswith("RX")
        print(f"\n[开方] 处方号 {rx.code}，状态 {rx.status}")


def test_cannot_choose_pickup_before_paying(engine, sample):
    """没缴费就想选取药时间 —— 必须被拒。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    with Session(engine) as db:
        with pytest.raises(pharmacy.InvalidState):
            pharmacy.choose_pickup(db, rx_id, date.today(), pharmacy.PICKUP_PERIODS[0])
    print("\n[状态机] 未缴费时选取药时间被拒 ✅")


def test_pay_then_choose_then_pick(engine, sample):
    """完整链路：缴费 → 选取药时间 → 取走。"""
    rx_id = _make_prescription(engine, sample, [
        {"medicine_id": sample["med_a"], "quantity": 2, "dosage": "口服，一次1粒"},
        {"medicine_id": sample["med_b"], "quantity": 1},
    ])
    before_a = _stock(engine, sample["med_a"])
    before_b = _stock(engine, sample["med_b"])

    with Session(engine) as db:
        rx = pharmacy.pay(db, rx_id)
        assert rx.status == "pending_pickup"
        print(f"\n[缴费] 状态 -> {rx.status}，金额 {rx.total_cents} 分")

        rx = pharmacy.choose_pickup(db, rx_id, date.today() + timedelta(days=1),
                                    pharmacy.PICKUP_PERIODS[1])
        assert rx.pickup_period == pharmacy.PICKUP_PERIODS[1]
        print(f"[选时段] {rx.pickup_date} {rx.pickup_period}")

        rx = pharmacy.mark_picked(db, rx_id)
        assert rx.status == "picked"
        print(f"[取药] 状态 -> {rx.status}")

    assert _stock(engine, sample["med_a"]) == before_a - 2
    assert _stock(engine, sample["med_b"]) == before_b - 1
    print(f"[库存] 药A {before_a}->{before_a - 2}，药B {before_b}->{before_b - 1}")


def test_cannot_pay_twice(engine, sample):
    """重复缴费要挡住，否则会重复扣库存。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    with Session(engine) as db:
        pharmacy.pay(db, rx_id)
        before = _stock(engine, sample["med_a"])
        with pytest.raises(pharmacy.InvalidState):
            pharmacy.pay(db, rx_id)
    assert _stock(engine, sample["med_a"]) == before, "重复缴费不应再扣库存"
    print(f"\n[幂等] 重复缴费被拒，库存保持 {before} ✅")


def test_cancel_returns_stock(engine, sample):
    """已缴费的处方作废，库存要还回去。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 3}])
    before = _stock(engine, sample["med_a"])

    with Session(engine) as db:
        pharmacy.pay(db, rx_id)
        assert _stock(engine, sample["med_a"]) == before - 3
        pharmacy.cancel(db, rx_id)
        assert _stock(engine, sample["med_a"]) == before, "作废后库存应复原"
    print(f"\n[作废] 库存 {before} -> {before - 3} -> {before} ✅")


def test_cannot_cancel_after_picked(engine, sample):
    """药已经取走了就不能作废。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    with Session(engine) as db:
        pharmacy.pay(db, rx_id)
        pharmacy.mark_picked(db, rx_id)
        with pytest.raises(pharmacy.InvalidState):
            pharmacy.cancel(db, rx_id)
    print("\n[状态机] 已取药的处方不能作废 ✅")


# --------------------------------------------------------------------------
# 2. 金额：整数分
# --------------------------------------------------------------------------
def test_amount_uses_integer_cents(engine, sample):
    """金额必须精确到分，不能出现浮点误差。

    药A 1234 分 * 3 = 3702 分；药B 56 分 * 7 = 392 分；合计 4094 分 = 40.94 元。
    """
    rx_id = _make_prescription(engine, sample, [
        {"medicine_id": sample["med_a"], "quantity": 3},
        {"medicine_id": sample["med_b"], "quantity": 7},
    ])
    with Session(engine) as db:
        rx = pharmacy.get(db, rx_id)
        assert rx.total_cents == 1234 * 3 + 56 * 7 == 4094
        detail = pharmacy.describe(rx)
        assert detail["total_yuan"] == 40.94
        print(f"\n[金额] 合计 {rx.total_cents} 分 = {detail['total_yuan']} 元")
        for item in detail["items"]:
            print(f"   {item['name']} {item['quantity']}{item['unit']} "
                  f"× {item['unit_price_yuan']} = {item['subtotal_yuan']} 元")


def test_price_is_snapshotted(engine, sample):
    """药品调价后，老处方的金额不能跟着变。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 2}])
    with Session(engine) as db:
        assert pharmacy.get(db, rx_id).total_cents == 1234 * 2

        # 药房调价
        med = db.get(Medicine, sample["med_a"])
        med.price_cents = 9999
        db.commit()

        assert pharmacy.get(db, rx_id).total_cents == 1234 * 2, "历史处方金额被改动了"
        print(f"\n[价格快照] 药品调价到 99.99 元后，老处方仍是 {1234 * 2} 分 ✅")


# --------------------------------------------------------------------------
# 3. 库存不足 与 并发
# --------------------------------------------------------------------------
def test_out_of_stock_blocks_payment(engine, sample):
    """库存不够时缴费失败，且不能扣掉一半。"""
    rx_id = _make_prescription(engine, sample, [
        {"medicine_id": sample["med_a"], "quantity": 2},   # 够
        {"medicine_id": sample["med_b"], "quantity": 99},  # 不够
    ])
    before_a = _stock(engine, sample["med_a"])
    before_b = _stock(engine, sample["med_b"])

    with Session(engine) as db:
        with pytest.raises(pharmacy.OutOfStock):
            pharmacy.pay(db, rx_id)
        db.rollback()

    # 关键：整个事务回滚，药A 不能被扣掉
    assert _stock(engine, sample["med_a"]) == before_a, "药A 被扣了，事务没回滚干净"
    assert _stock(engine, sample["med_b"]) == before_b
    print(f"\n[库存不足] 缴费失败，药A 保持 {before_a}（未出现扣一半）✅")


def test_concurrent_payment_never_oversells(engine, sample):
    """并发缴费不能把库存扣成负数 —— 药发出去比号源超卖更难收场。"""
    medicine_id = sample["med_a"]
    total_stock = _stock(engine, medicine_id)          # 10 盒

    # 做 30 张处方，每张要 1 盒；最多只能有 10 张缴费成功
    rx_ids = [
        _make_prescription(engine, sample, [{"medicine_id": medicine_id, "quantity": 1}])
        for _ in range(30)
    ]

    ok = [0]
    failed = [0]
    lock = threading.Lock()
    start = threading.Event()

    def worker(rx_id: int) -> None:
        start.wait()
        with Session(engine) as db:
            try:
                pharmacy.pay(db, rx_id)
                with lock:
                    ok[0] += 1
            except pharmacy.OutOfStock:
                with lock:
                    failed[0] += 1
            except Exception as exc:                    # noqa: BLE001
                db.rollback()
                with lock:
                    failed[0] += 1
                print(f"  意外异常 {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, rx) for rx in rx_ids]
        start.set()
        for f in futures:
            f.result()

    remaining = _stock(engine, medicine_id)
    paid_count = None
    with Session(engine) as db:
        paid_count = len(list(db.scalars(
            select(Prescription).where(Prescription.status == "pending_pickup"))))

    print(f"\n[并发缴费] 库存 {total_stock}，30 张处方抢：成功 {ok[0]}，失败 {failed[0]}，"
          f"剩余 {remaining}，待取药处方 {paid_count}")
    assert ok[0] == total_stock, f"应恰好成功 {total_stock} 张，实际 {ok[0]}"
    assert remaining == 0, f"库存应放完，实际 {remaining}"
    assert remaining >= 0, "库存被扣成负数 —— 超发了"


def test_low_stock_list(engine, sample):
    """库存低于警戒线的药要能列出来给药房。"""
    with Session(engine) as db:
        med = db.get(Medicine, sample["med_b"])       # stock=5, warn=2
        assert med not in pharmacy.low_stock(db)

        med.stock = 2                                   # 触线
        db.commit()
        low = pharmacy.low_stock(db)
        assert any(m.id == med.id for m in low)
        print(f"\n[补货提醒] 药B 降到 {med.stock} 后进入补货列表 ✅")


def test_medicine_not_found(engine, sample):
    with Session(engine) as db:
        with pytest.raises(pharmacy.MedicineNotFound):
            pharmacy.create_prescription(
                db, patient_id=sample["patient_id"], doctor_id=sample["doctor_id"],
                items=[{"medicine_id": 999999, "quantity": 1}])
    print("\n[校验] 不存在的药品被拒 ✅")


# --------------------------------------------------------------------------
# 4. 删除处方：只允许走统一入口
# --------------------------------------------------------------------------
def test_delete_prescriptions_removes_items_too(engine, sample):
    """删处方必须把明细一起删掉，且不能触发外键约束失败。

    背景：项目里曾在三个脚本各写一份删除逻辑，三份都忘了先删明细，
    同一个 FOREIGN KEY constraint failed 犯了三次。抽成统一入口之后，
    这条测试负责钉住"先明细后主表"的顺序。
    """
    from app.domain.models import PrescriptionItem

    rx_id = _make_prescription(engine, sample, [
        {"medicine_id": sample["med_a"], "quantity": 1},
        {"medicine_id": sample["med_b"], "quantity": 1},
    ])

    with Session(engine) as db:
        before = len(list(db.scalars(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx_id))))
        assert before == 2

        removed = pharmacy.delete_prescriptions(db, [rx_id])
        db.commit()

        items_left = len(list(db.scalars(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx_id))))
        rx_left = db.get(Prescription, rx_id)

    print(f"\n[删处方] 返回 {removed}，剩余处方 {rx_left}，剩余明细 {items_left}")
    assert removed == 1
    assert rx_left is None, "处方没删掉"
    assert items_left == 0, "明细没跟着删 —— 这正是那个反复犯的 bug"


def test_delete_naive_order_would_fail(engine, sample):
    """反证：绕过统一入口、先删主表，就会撞外键。

    这条测试故意用错误顺序，证明"为什么必须走统一入口"。
    如果哪天外键被关掉了，它会失败 —— 说明兜底机制没了。
    """
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    from app.domain.models import PrescriptionItem

    rx_id = _make_prescription(engine, sample, [
        {"medicine_id": sample["med_a"], "quantity": 1}])

    with Session(engine) as db:
        with pytest.raises(IntegrityError):
            # 错误顺序：先删主表，明细还在引用它
            db.execute(delete(Prescription).where(Prescription.id == rx_id))
            db.flush()
        db.rollback()

        # 确认数据没被破坏
        assert db.get(Prescription, rx_id) is not None
        assert len(list(db.scalars(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx_id)))) == 1
    print("\n[反证] 先删主表确实会撞外键，统一入口是必要的 ✅")


def test_delete_prescriptions_with_empty_list(engine, sample):
    """传空列表要安全返回 0，而不是把整表删了（这是很容易写出的严重 bug）。"""
    rx_id = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    with Session(engine) as db:
        removed = pharmacy.delete_prescriptions(db, [])
        db.commit()
        still_there = db.get(Prescription, rx_id)
    print(f"\n[空列表] 返回 {removed}，处方还在：{still_there is not None}")
    assert removed == 0
    assert still_there is not None, "传空列表把数据删光了 —— 严重 bug"


def test_delete_all_prescriptions(engine, sample):
    """传 None 表示全删（重置演示数据时用）。"""
    for _ in range(3):
        _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    with Session(engine) as db:
        removed = pharmacy.delete_prescriptions(db)
        db.commit()
        left = len(list(db.scalars(select(Prescription))))
    print(f"\n[全删] 删除 {removed} 张，剩余 {left}")
    assert removed == 3
    assert left == 0


# --------------------------------------------------------------------------
# 5. 开方号：并发不撞号
# --------------------------------------------------------------------------
def test_prescription_code_survives_concurrent_creation(engine, sample):
    """多个医生同时开方，号不能撞。

    以前 _next_code 是"当天记录数 + 1"，删过处方之后还会算重。
    改成取当天最大号 +1，并发撞号靠唯一约束加一次重试兜住。
    """
    n = 24

    def worker(_i: int) -> str | None:
        with Session(engine) as db:
            try:
                rx = pharmacy.create_prescription(
                    db, patient_id=sample["patient_id"], doctor_id=sample["doctor_id"],
                    items=[{"medicine_id": sample["med_a"], "quantity": 1}])
                return rx.code
            except Exception as exc:                    # noqa: BLE001
                db.rollback()
                print(f"  开方失败 {type(exc).__name__}: {exc}")
                return None

    start = threading.Event()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(lambda i=i: (start.wait(), worker(i))[1]) for i in range(n)]
        start.set()
        codes = [f.result() for f in futures]

    ok = [c for c in codes if c]
    with Session(engine) as db:
        stored = [r.code for r in db.scalars(select(Prescription))]

    print(f"\n[并发开方] {n} 个并发：成功 {len(ok)}，落库 {len(stored)}，"
          f"唯一号 {len(set(stored))}")
    assert len(ok) == n, f"有 {n - len(ok)} 个开方失败（撞号没兜住）"
    assert len(set(stored)) == n, f"落库的号有重复：{sorted(stored)}"


def test_code_does_not_reuse_after_delete(engine, sample):
    """删掉处方后再开方，号不能和已存在的撞上。

    这正是"数记录条数"写法的 bug：删一张后数量少 1，
    新号会和当天已有的最后一张重复。
    """
    first = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])
    second = _make_prescription(engine, sample, [{"medicine_id": sample["med_a"], "quantity": 1}])

    with Session(engine) as db:
        code1 = db.get(Prescription, first).code
        code2 = db.get(Prescription, second).code
        assert code1 != code2

        # 删掉第一张，此时当天记录数回到 1
        pharmacy.delete_prescriptions(db, [first])
        db.commit()

        # 旧写法这里会生成和 code2 相同的号
        third = pharmacy.create_prescription(
            db, patient_id=sample["patient_id"], doctor_id=sample["doctor_id"],
            items=[{"medicine_id": sample["med_a"], "quantity": 1}])
        code3 = third.code

    print(f"\n[删后重开] {code1} / {code2} / {code3}")
    assert code3 != code2, f"删掉 {code1} 后新号 {code3} 和已有的 {code2} 撞了"
    assert len({code1, code2, code3}) == 3
