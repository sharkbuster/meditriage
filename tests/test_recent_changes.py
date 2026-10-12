"""针对最近改动的对抗测试。

改了两块：开方号从"查再算"换成自增 id 生成，以及删除处方收敛到一个入口。
这两处都容易留坑，所以单独打一轮：
格式化边界、重试残留、删除的边界参数、状态机跳步。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import Base, enable_sqlite_pragmas, get_db
from app.domain.models import (
    Department,
    Doctor,
    Medicine,
    Patient,
    Prescription,
    PrescriptionItem,
    Schedule,
)
from app.main import app
from app.services import pharmacy


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'recent.db'}", future=True, poolclass=NullPool)

    @event.listens_for(eng, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        enable_sqlite_pragmas(dbapi_conn)
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def data(engine):
    with Session(engine) as db:
        dep = Department(name="内科", keywords="测试", description="测试")
        db.add(dep)
        db.flush()
        doc = Doctor(name="医生甲", title="主治医师", department_id=dep.id)
        db.add(doc)
        db.flush()
        patient = Patient(name="张三", phone="13800002222", id_card="460100200401012222")
        db.add(patient)
        db.flush()
        med = Medicine(name="药甲", spec="10片", unit="盒", price_cents=500, stock=100)
        db.add(med)
        db.commit()
        return {"dep": dep.id, "doctor": doc.id, "patient": patient.id, "med": med.id}


def _rx(db, data, qty=1):
    return pharmacy.create_prescription(
        db, patient_id=data["patient"], doctor_id=data["doctor"],
        items=[{"medicine_id": data["med"], "quantity": qty}])


# ==========================================================================
# 一、开方号格式与边界
# ==========================================================================
def test_code_format_is_stable(engine, data):
    """号形如 RX + 8 位日期 + 6 位序号，长度固定 16。"""
    with Session(engine) as db:
        rx = _rx(db, data)
        code = rx.code
    print(f"\n[号格式] {code}（长度 {len(code)}）")
    assert code.startswith(f"RX{date.today():%Y%m%d}")
    assert len(code) == 16, f"号长度应为 16，实际 {len(code)}"
    assert code[2:10].isdigit() and code[10:].isdigit()


def test_code_sorts_and_pads_correctly(engine, data):
    """序号要零填充，否则 '10' 会排在 '2' 前面，药房按号排序就乱。"""
    codes = []
    with Session(engine) as db:
        for _ in range(12):
            codes.append(_rx(db, data).code)
    seqs = [c[10:] for c in codes]
    print(f"\n[零填充] 序号序列 {seqs}")
    assert seqs == [f"{i:06d}" for i in range(1, 13)], "序号没有按 6 位零填充递增"
    assert codes == sorted(codes), "按字符串排序时顺序乱了"


def test_code_beyond_9999_does_not_truncate(engine, data):
    """序号破万时既不能截断，也不能让排序乱掉。

    之前补 4 位，id 到 10000 时 "10000" < "9999"（字符串比较），
    按号排序会把新号排到旧号前面。这条测试就是把它钉住。
    """
    small = pharmacy._make_code(9999)
    big = pharmacy._make_code(10000)
    huge = pharmacy._make_code(999999)
    print(f"\n[破万] {small} / {big} / {huge}")
    assert small[10:] == "009999"
    assert big[10:] == "010000", f"序号被截断了：{big}"
    assert huge[10:] == "999999"
    assert len({small, big, huge}) == 3, "号撞了"
    # 字符串排序必须和数值顺序一致
    assert sorted([huge, small, big]) == [small, big, huge], "号排序乱了"


def test_code_column_fits_large_id(engine, data):
    """id 很大时号要能存进去，不能超长被截断。"""
    with Session(engine) as db:
        code = pharmacy._make_code(99999999)
        rx = Prescription(code=code, patient_id=data["patient"], doctor_id=data["doctor"],
                          status="pending_pay", total_cents=0)
        db.add(rx)
        db.commit()
        stored = db.get(Prescription, rx.id).code
    print(f"\n[大号入库] {code}（长度 {len(code)}）-> 存回 {stored}")
    assert stored == code, "号被截断了"


def test_code_unique_across_many(engine, data):
    """连开 60 张，号必须全唯一（覆盖多批次 flush）。"""
    with Session(engine) as db:
        codes = [_rx(db, data).code for _ in range(60)]
        stored = [r.code for r in db.scalars(select(Prescription))]
    print(f"\n[连开 60 张] 生成 {len(set(codes))} 个唯一号，落库 {len(stored)} 张")
    assert len(set(codes)) == 60
    assert len(set(stored)) == 60


def test_no_retry_leftovers_in_module():
    """重试逻辑已经删掉了，模块里不该再留 MAX_CODE_RETRY / _attempt。"""
    src = (pharmacy.__file__)
    text_src = open(src, encoding="utf-8").read()
    for token in ("MAX_CODE_RETRY", "_attempt", "_next_code"):
        assert token not in text_src, f"还残留 {token}，说明改动没清干净"
    print("\n[清理] 重试逻辑残留已清空 ✅")


def test_create_prescription_rolls_back_on_bad_item(engine, data):
    """一味药不存在时整单失败，不能留下半张没有明细的处方。"""
    with Session(engine) as db:
        before = db.scalar(select(func.count()).select_from(Prescription))
        with pytest.raises(pharmacy.MedicineNotFound):
            pharmacy.create_prescription(
                db, patient_id=data["patient"], doctor_id=data["doctor"],
                items=[{"medicine_id": data["med"], "quantity": 1},
                       {"medicine_id": 999999, "quantity": 1}])
        db.rollback()
        after = db.scalar(select(func.count()).select_from(Prescription))
    print(f"\n[半张处方] 处方数 {before} -> {after}")
    assert after == before, "失败的开方留下了残缺处方"


# ==========================================================================
# 二、删除入口的边界
# ==========================================================================
def test_delete_nonexistent_ids(engine, data):
    """删一批不存在的 id，应返回 0 而不是报错。"""
    with Session(engine) as db:
        removed = pharmacy.delete_prescriptions(db, [999999])
        db.commit()
    print(f"\n[删不存在的 id] 返回 {removed}")
    assert removed == 0


def test_delete_mixed_existing_and_missing(engine, data):
    """一半存在一半不存在，存在的要删掉、返回数要准确。"""
    with Session(engine) as db:
        ids = [_rx(db, data).id for _ in range(3)]
        removed = pharmacy.delete_prescriptions(db, ids + [999999])
        db.commit()
        left = db.scalar(select(func.count()).select_from(Prescription))
    print(f"\n[混合删除] 传入 4 个（3 真 1 假），返回 {removed}，剩余 {left}")
    assert removed == 3
    assert left == 0


def test_delete_prescription_with_many_items(engine, data):
    """明细多的处方也要一次删干净，不能只删掉一部分。"""
    with Session(engine) as db:
        with Session(engine) as db_inner:
            pass
        rx = _rx(db, data)
        # 手工加一堆明细
        for _ in range(11):
            db.add(PrescriptionItem(prescription_id=rx.id, medicine_id=data["med"],
                                    quantity=1, dosage="", unit_price_cents=500))
        db.commit()
        total_items = db.scalar(
            select(func.count()).select_from(PrescriptionItem)
            .where(PrescriptionItem.prescription_id == rx.id))
        assert total_items == 12

        pharmacy.delete_prescriptions(db, [rx.id])
        db.commit()
        left_items = db.scalar(
            select(func.count()).select_from(PrescriptionItem)
            .where(PrescriptionItem.prescription_id == rx.id))
    print(f"\n[多明细] 删除前 {total_items} 条明细，删除后剩 {left_items}")
    assert left_items == 0, "有明细没删掉，会变成孤儿行"


def test_delete_does_not_touch_other_tables(engine, data):
    """删处方不能顺手动到药品或患者。"""
    with Session(engine) as db:
        rx = _rx(db, data)
        pharmacy.delete_prescriptions(db, [rx.id])
        db.commit()
        meds = db.scalar(select(func.count()).select_from(Medicine))
        patients = db.scalar(select(func.count()).select_from(Patient))
        doctors = db.scalar(select(func.count()).select_from(Doctor))
    print(f"\n[误删检查] 药品 {meds} / 患者 {patients} / 医生 {doctors}")
    assert meds == 1 and patients == 1 and doctors == 1


# ==========================================================================
# 三、状态机跳步
# ==========================================================================
def test_cannot_pick_without_choosing_time(engine, data):
    """没选取药时间就直接发药 —— 应该允许还是拦住？

    这里记录当前行为：允许。药房现场发药时患者可能没在线上选时段，
    所以不拦。但状态必须正确落到 picked。
    """
    with Session(engine) as db:
        rx = _rx(db, data)
        pharmacy.pay(db, rx.id)
        done = pharmacy.mark_picked(db, rx.id)
    print(f"\n[未选时段直接发药] 状态 -> {done.status}")
    assert done.status == "picked"


def test_can_change_pickup_time_twice(engine, data):
    """改取药时间应该允许（患者临时有事很常见）。

    注意别在一个 session 里比较两次返回的对象：它们是同一个实例，
    读出来都是最后一次的值，断言等于什么都没验。所以分两次查。
    """
    with Session(engine) as db:
        rx = _rx(db, data)
        pharmacy.pay(db, rx.id)
        rx_id = rx.id
        pharmacy.choose_pickup(db, rx_id, date.today(), pharmacy.PICKUP_PERIODS[0])

    with Session(engine) as db:
        first = pharmacy.get(db, rx_id)
        first_state = (first.pickup_date, first.pickup_period)
        pharmacy.choose_pickup(db, rx_id, date.today() + timedelta(days=1),
                               pharmacy.PICKUP_PERIODS[1])

    with Session(engine) as db:
        second = pharmacy.get(db, rx_id)
        second_state = (second.pickup_date, second.pickup_period)

    print(f"\n[改时段] 第一次 {first_state} -> 第二次 {second_state}")
    assert first_state == (date.today(), pharmacy.PICKUP_PERIODS[0]), "第一次没生效"
    assert second_state == (date.today() + timedelta(days=1), pharmacy.PICKUP_PERIODS[1])


def test_cancel_after_pay_then_rebook_same_prescription(engine, data):
    """作废后不能再发药，也不能再选时段。"""
    with Session(engine) as db:
        rx = _rx(db, data)
        pharmacy.pay(db, rx.id)
        pharmacy.cancel(db, rx.id)
        with pytest.raises(pharmacy.InvalidState):
            pharmacy.mark_picked(db, rx.id)
        with pytest.raises(pharmacy.InvalidState):
            pharmacy.choose_pickup(db, rx.id, date.today(), pharmacy.PICKUP_PERIODS[0])
    print("\n[作废后] 发药和选时段都被拒 ✅")


def test_stock_reflects_pay_and_cancel(engine, data):
    """缴费扣库存、作废还库存，来回一次库存必须回到原值。"""
    with Session(engine) as db:
        start = db.get(Medicine, data["med"]).stock
        for _ in range(3):
            rx = _rx(db, data, qty=2)
            pharmacy.pay(db, rx.id)
            assert db.get(Medicine, data["med"]).stock == start - 2
            pharmacy.cancel(db, rx.id)
            assert db.get(Medicine, data["med"]).stock == start, "库存没还原"
    print(f"\n[缴费作废 3 轮] 库存稳定在 {start}")


# ==========================================================================
# 四、接口层：开方号要出现在返回里
# ==========================================================================
def test_api_returns_new_code_format(client, data):
    resp = client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"],
        "items": [{"medicine_id": data["med"], "quantity": 1}]})
    assert resp.status_code == 201, resp.text
    code = resp.json()["code"]
    print(f"\n[接口返回] 处方号 {code}")
    assert code.startswith(f"RX{date.today():%Y%m%d}")
    assert len(code) == 16


def test_api_prescription_list_shows_code(client, data):
    client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"],
        "items": [{"medicine_id": data["med"], "quantity": 1}]})
    listed = client.get("/api/prescriptions?phone=13800002222").json()
    print(f"\n[列表] {len(listed)} 张，号 {[r['code'] for r in listed]}")
    assert len(listed) == 1
    assert listed[0]["code"].startswith("RX")


def test_schedule_flow_untouched_by_changes(client, engine, data):
    """顺手确认预约那条链路没被最近的改动碰到。"""
    with Session(engine) as db:
        import datetime as dt
        sch = Schedule(doctor_id=data["doctor"], work_date=date.today() + timedelta(days=1),
                       period="上午", start_time=dt.time(8, 30), end_time=dt.time(11, 30),
                       capacity=2, remaining=2)
        db.add(sch)
        db.commit()
        sid = sch.id

    r1 = client.post("/api/appointments",
                     json={"name": "李四", "phone": "13800003333", "schedule_id": sid})
    r2 = client.post("/api/appointments",
                     json={"name": "李四", "phone": "13800003333", "schedule_id": sid})
    print(f"\n[预约链路] 第一次 {r1.status_code}，重复 {r2.status_code}")
    assert r1.status_code == 201 and r2.status_code == 409
