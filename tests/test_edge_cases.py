"""对抗性测试：专挑边界、异常路径、以及"没人会这么用但真会发生"的情况。

目的不是证明代码能跑，而是**想办法把它弄坏**。
每条测试都对应一个真实的医院场景，注释里写了"谁会这么干"。

跑法：
    .venv\\Scripts\\python.exe -m pytest tests/test_edge_cases.py -v -s
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
    Appointment,
    Department,
    Doctor,
    Medicine,
    Patient,
    Prescription,
    Schedule,
)
from app.main import app
from app.services import pharmacy


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'edge.db'}", future=True, poolclass=NullPool)

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
    """一份最小可用数据：1 科室 / 1 医生 / 1 排班(3 号) / 1 患者 / 2 种药。"""
    with Session(engine) as db:
        dep = Department(name="测试科", keywords="测试", description="测试")
        db.add(dep)
        db.flush()
        doc = Doctor(name="测试医生", title="主治医师", department_id=dep.id)
        db.add(doc)
        db.flush()
        sch = Schedule(doctor_id=doc.id, work_date=date.today() + timedelta(days=1),
                       period="上午", start_time=__import__("datetime").time(8, 30),
                       end_time=__import__("datetime").time(11, 30),
                       capacity=3, remaining=3)
        db.add(sch)
        db.flush()
        patient = Patient(name="张三", phone="13800001111", id_card="460100200401011111")
        db.add(patient)
        db.flush()
        med = Medicine(name="测试药", spec="10片", unit="盒",
                       price_cents=1000, stock=5, warn_stock=2)
        db.add(med)
        db.commit()
        return {"dept": dep.id, "doctor": doc.id, "schedule": sch.id,
                "patient": patient.id, "medicine": med.id,
                "phone": "13800001111"}


def _book(client, schedule_id, phone="13800001111", name="张三"):
    return client.post("/api/appointments",
                       json={"name": name, "phone": phone, "schedule_id": schedule_id})


# ==========================================================================
# 一、输入校验：乱填会发生什么
# ==========================================================================
def test_triage_with_empty_symptom(client):
    """患者直接点按钮不填内容。"""
    resp = client.post("/api/triage", json={"symptom": ""})
    print(f"\n[空症状] HTTP {resp.status_code}")
    assert resp.status_code == 422, "空症状应被校验挡住"


def test_triage_with_whitespace_only(client):
    """患者敲了几个空格。"""
    resp = client.post("/api/triage", json={"symptom": "   "})
    print(f"[纯空格] HTTP {resp.status_code}")
    assert resp.status_code == 422, "纯空格应按空处理"


def test_triage_with_emoji_and_punctuation(client):
    """患者发了一堆表情和符号，没有实际内容。"""
    resp = client.post("/api/triage", json={"symptom": "。。。！！！？？？😭😭"})
    body = resp.json()
    print(f"[无意义符号] HTTP {resp.status_code} "
          f"need_human={body.get('need_human')} 候选={len(body.get('candidates', []))}")
    assert resp.status_code == 200
    # 关键：不能瞎推荐一个科室，应该转人工
    assert body["need_human"] is True, "无意义输入不该给出确定的科室推荐"


def test_triage_with_very_long_symptom(client):
    """患者把症状写成了小作文。"""
    long_text = "咳嗽发烧" * 200          # 800 字
    resp = client.post("/api/triage", json={"symptom": long_text})
    print(f"[超长主诉] {len(long_text)} 字 -> HTTP {resp.status_code}")
    assert resp.status_code == 422, "超过 500 字上限应被挡住"


def test_triage_with_mixed_department_conflict(client):
    """症状同时指向两个科室（真实场景：患者有好几种毛病）。"""
    resp = client.post("/api/triage", json={"symptom": "牙疼而且咳嗽发烧"})
    body = resp.json()
    names = [c["department_name"] for c in body["candidates"]]
    print(f"[症状跨科室] 候选={names} 覆盖率={body['confidence']} "
          f"转人工={body['need_human']}")
    assert resp.status_code == 200
    assert len(body["candidates"]) >= 2, "跨科室症状应给出多个候选，而不是只报一个"


def test_triage_top_k_bounds(client):
    """top_k 越界。"""
    for k in (0, 6, -1):
        resp = client.post("/api/triage", json={"symptom": "咳嗽", "top_k": k})
        print(f"[top_k={k}] HTTP {resp.status_code}")
        assert resp.status_code == 422, f"top_k={k} 应被校验挡住"


# ==========================================================================
# 二、身份与归属：能不能看到别人的东西
# ==========================================================================
def test_unknown_phone_sees_nothing(client, data):
    """陌生手机号查预约/处方 —— 必须是空，不能报错也不能漏数据。"""
    r1 = client.get("/api/appointments?phone=13900009999")
    r2 = client.get("/api/prescriptions?phone=13900009999")
    print(f"\n[陌生号码] 预约 HTTP {r1.status_code}({len(r1.json())} 条) "
          f"处方 HTTP {r2.status_code}({len(r2.json())} 条)")
    assert r1.status_code == 200 and r1.json() == []
    assert r2.status_code == 200 and r2.json() == []


def test_phone_format_rejected(client):
    """各种非法手机号。"""
    bad = ["139", "12345678901", "abcdefghijk", "1390000111a", "139000011112"]
    for phone in bad:
        resp = client.get(f"/api/appointments?phone={phone}")
        print(f"[手机号 {phone!r}] HTTP {resp.status_code}")
        assert resp.status_code == 422, f"{phone!r} 应被拒"


def test_name_too_long_rejected(client, data):
    """姓名超长（粘贴了整段文字）。"""
    resp = _book(client, data["schedule"], name="张" * 30)
    print(f"\n[超长姓名] HTTP {resp.status_code}")
    assert resp.status_code == 422


def test_appointment_for_nonexistent_schedule(client, data):
    """排班 id 不存在。"""
    resp = _book(client, 999999)
    print(f"\n[不存在的排班] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 404


def test_cancel_nonexistent_appointment(client, data):
    resp = client.post("/api/appointments/999999/cancel")
    print(f"\n[取消不存在的预约] HTTP {resp.status_code}")
    assert resp.status_code == 404


# ==========================================================================
# 三、重复操作：连点、手抖、网络重试
# ==========================================================================
def test_double_cancel_does_not_double_release(client, data):
    """取消按钮连点两次 —— 号源不能被归还两次。"""
    appt_id = _book(client, data["schedule"]).json()["id"]
    r1 = client.post(f"/api/appointments/{appt_id}/cancel")
    r2 = client.post(f"/api/appointments/{appt_id}/cancel")
    remaining = client.get("/api/schedules").json()[0]["remaining"]
    print(f"\n[重复取消] 第一次 {r1.status_code}，第二次 {r2.status_code}，"
          f"号源={remaining}（容量 3）")
    assert r1.status_code == 200 and r2.status_code == 200
    assert remaining == 3, f"号源被重复归还了，实际 {remaining}"


def test_duplicate_prescription_pay(client, engine, data):
    """缴费按钮连点两次。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])
        rx_id = rx.id

    r1 = client.post(f"/api/prescriptions/{rx_id}/pay")
    r2 = client.post(f"/api/prescriptions/{rx_id}/pay")
    with Session(engine) as db:
        stock = db.get(Medicine, data["medicine"]).stock
    print(f"\n[重复缴费] 第一次 {r1.status_code}，第二次 {r2.status_code}，库存={stock}（初始 5）")
    assert r1.status_code == 200
    assert r2.status_code == 409, "重复缴费应被拒"
    assert stock == 4, f"库存被扣了两次，实际 {stock}"


def test_duplicate_mark_picked(client, engine, data):
    """药房发药按钮连点两次。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])
        rx_id = rx.id
    client.post(f"/api/prescriptions/{rx_id}/pay")

    r1 = client.post(f"/api/prescriptions/{rx_id}/picked")
    r2 = client.post(f"/api/prescriptions/{rx_id}/picked")
    print(f"\n[重复发药] 第一次 {r1.status_code}，第二次 {r2.status_code}")
    assert r1.status_code == 200
    assert r2.status_code == 409, "重复发药应被拒"


def test_cancel_paid_prescription_twice(client, engine, data):
    """作废连点两次 —— 库存不能被还两次。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 2}])
        rx_id = rx.id
    client.post(f"/api/prescriptions/{rx_id}/pay")

    r1 = client.post(f"/api/prescriptions/{rx_id}/cancel")
    r2 = client.post(f"/api/prescriptions/{rx_id}/cancel")
    with Session(engine) as db:
        stock = db.get(Medicine, data["medicine"]).stock
    print(f"\n[重复作废] 第一次 {r1.status_code}，第二次 {r2.status_code}，库存={stock}（初始 5）")
    assert r1.status_code == 200
    assert r2.status_code == 200, "重复作废应幂等返回，而不是报错"
    assert stock == 5, f"库存被归还两次，实际 {stock}"


# ==========================================================================
# 四、取药时间：过去、太远、乱填
# ==========================================================================
@pytest.mark.parametrize("offset,label", [(-1, "昨天"), (30, "30天后")])
def test_pickup_date_out_of_range(client, engine, data, offset, label):
    """选过去的时间，或者选一个月后。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])
        rx_id = rx.id
    client.post(f"/api/prescriptions/{rx_id}/pay")

    target = (date.today() + timedelta(days=offset)).isoformat()
    resp = client.post(f"/api/prescriptions/{rx_id}/pickup",
                       json={"pickup_date": target, "period": pharmacy.PICKUP_PERIODS[0]})
    print(f"\n[取药时间 {label}] HTTP {resp.status_code} "
          f"detail={resp.json().get('detail')}")
    assert resp.status_code == 409, f"{label}不该被接受"


def test_pickup_with_bogus_period(client, engine, data):
    """前端被改过，或者有人直接调接口传了个不存在的时段。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])
        rx_id = rx.id
    client.post(f"/api/prescriptions/{rx_id}/pay")

    resp = client.post(f"/api/prescriptions/{rx_id}/pickup",
                       json={"pickup_date": date.today().isoformat(),
                             "period": "凌晨三点"})
    print(f"\n[非法时段] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 409


def test_pickup_boundary_today_is_allowed(client, engine, data):
    """边界：今天取药应该允许（患者看完病当场取）。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])
        rx_id = rx.id
    client.post(f"/api/prescriptions/{rx_id}/pay")

    resp = client.post(f"/api/prescriptions/{rx_id}/pickup",
                       json={"pickup_date": date.today().isoformat(),
                             "period": pharmacy.PICKUP_PERIODS[0]})
    print(f"\n[今天取药] HTTP {resp.status_code}")
    assert resp.status_code == 200, "今天取药应该允许"


# ==========================================================================
# 五、开方与库存：数量、缺药、边界
# ==========================================================================
def test_prescription_with_zero_quantity(client, data):
    """医生手滑填了 0。"""
    resp = client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"],
        "items": [{"medicine_id": data["medicine"], "quantity": 0}],
    })
    print(f"\n[数量 0] HTTP {resp.status_code}")
    assert resp.status_code == 422


def test_prescription_with_huge_quantity(client, data):
    """医生填了 9999 盒。"""
    resp = client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"],
        "items": [{"medicine_id": data["medicine"], "quantity": 9999}],
    })
    print(f"\n[数量 9999] HTTP {resp.status_code}")
    assert resp.status_code == 422, "单次开药数量应有上限"


def test_prescription_with_empty_items(client, data):
    """开了张没有药的处方。"""
    resp = client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"], "items": [],
    })
    print(f"\n[空处方] HTTP {resp.status_code}")
    assert resp.status_code == 422


def test_prescription_for_nonexistent_patient(client, data):
    resp = client.post("/api/prescriptions", json={
        "patient_id": 999999, "doctor_id": data["doctor"],
        "items": [{"medicine_id": data["medicine"], "quantity": 1}],
    })
    print(f"\n[不存在的患者] HTTP {resp.status_code}")
    # 外键约束在 SQLite 内存库可能不生效，但至少要报错而不是静默成功
    assert resp.status_code in (404, 409, 422, 500), "应报错"


def test_duplicate_medicine_in_one_prescription(client, engine, data):
    """同一味药在一张处方里出现两次（医生分两行写了）。"""
    resp = client.post("/api/prescriptions", json={
        "patient_id": data["patient"], "doctor_id": data["doctor"],
        "items": [
            {"medicine_id": data["medicine"], "quantity": 1, "dosage": "一次1片"},
            {"medicine_id": data["medicine"], "quantity": 1, "dosage": "一次1片"},
        ],
    })
    print(f"\n[同一味药开两行] HTTP {resp.status_code}")
    assert resp.status_code == 201
    body = resp.json()
    print(f"   金额 {body['total_yuan']} 元，明细 {len(body['items'])} 行")
    assert body["total_yuan"] == 20.0, "两行应各自计价"

    # 缴费时应扣 2 盒而不是 1 盒
    client.post(f"/api/prescriptions/{body['id']}/pay")
    with Session(engine) as db:
        stock = db.get(Medicine, data["medicine"]).stock
    print(f"   缴费后库存 {stock}（初始 5，应扣 2）")
    assert stock == 3, f"同一味药分两行时应扣 2 盒，实际扣到 {stock}"


def test_pay_prescription_when_stock_insufficient(client, engine, data):
    """开方时够，缴费时被别人买光了。"""
    with Session(engine) as db:
        rx = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 3}])
        rx_id = rx.id
        # 别人把库存买走
        med = db.get(Medicine, data["medicine"])
        med.stock = 1
        db.commit()

    resp = client.post(f"/api/prescriptions/{rx_id}/pay")
    with Session(engine) as db:
        stock = db.get(Medicine, data["medicine"]).stock
    print(f"\n[缴费时库存不足] HTTP {resp.status_code} detail={resp.json().get('detail')} "
          f"库存={stock}")
    assert resp.status_code == 409
    assert stock == 1, "失败的缴费不该动库存"


# ==========================================================================
# 六、数据一致性：状态和库存对不对得上
# ==========================================================================
def test_stock_never_negative_after_mixed_operations(client, engine, data):
    """混合操作后库存不能为负，也不能凭空多出来。

    模拟：开 3 张处方 → 1 张缴费 → 1 张作废 → 1 张不管。
    初始库存 5，每张要 2 盒。
    """
    ids = []
    for _ in range(3):
        resp = client.post("/api/prescriptions", json={
            "patient_id": data["patient"], "doctor_id": data["doctor"],
            "items": [{"medicine_id": data["medicine"], "quantity": 2}],
        })
        ids.append(resp.json()["id"])

    client.post(f"/api/prescriptions/{ids[0]}/pay")       # 扣 2 -> 3
    client.post(f"/api/prescriptions/{ids[1]}/cancel")    # 未缴费，返还 0 -> 3
    # ids[2] 不动

    with Session(engine) as db:
        stock = db.get(Medicine, data["medicine"]).stock
        statuses = [db.get(Prescription, i).status for i in ids]
    print(f"\n[混合操作] 库存={stock}（初始 5）状态={statuses}")
    assert stock == 3, f"库存应为 3，实际 {stock}"
    assert stock >= 0, "库存变成负数"


def test_appointment_and_prescription_are_linked_by_phone(client, engine, data):
    """同一手机号：预约和处方都要能查到，不能串号。"""
    _book(client, data["schedule"])
    with Session(engine) as db:
        pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["medicine"], "quantity": 1}])

    appts = client.get(f"/api/appointments?phone={data['phone']}").json()
    rxs = client.get(f"/api/prescriptions?phone={data['phone']}").json()
    print(f"\n[同一手机号] 预约 {len(appts)} 条，处方 {len(rxs)} 张")
    assert len(appts) == 1 and len(rxs) == 1


def test_schedule_remaining_never_exceeds_capacity(client, engine, data):
    """反复预约+取消，号源不能被"刷"多于容量。"""
    for _ in range(5):
        appt = _book(client, data["schedule"]).json()
        client.post(f"/api/appointments/{appt['id']}/cancel")

    remaining = client.get("/api/schedules").json()[0]["remaining"]
    with Session(engine) as db:
        capacity = db.get(Schedule, data["schedule"]).capacity
    print(f"\n[反复预约取消 5 轮] 剩余={remaining} 容量={capacity}")
    assert remaining == capacity, f"号源被刷多了：{remaining} > {capacity}"


def test_only_available_filter_is_accurate(client, engine, data):
    """only_available=true 不能混进没号的排班。"""
    # 把唯一排班的号约满
    for i in range(3):
        _book(client, data["schedule"], phone=f"1380000{i:04d}", name=f"患者{i}")

    available = client.get("/api/schedules?only_available=true").json()
    with Session(engine) as db:
        remaining = db.get(Schedule, data["schedule"]).remaining
    print(f"\n[约满后查可约] 剩余={remaining}，接口返回 {len(available)} 条")
    assert remaining == 0
    assert all(s["remaining"] > 0 for s in available), "混进了没号的排班"


def test_medicine_search_special_characters(client, engine, data):
    """搜索框里输入 SQL 通配符或特殊字符。"""
    for keyword in ("%", "_", "'", "'; DROP TABLE medicine; --", "测试"):
        resp = client.get("/api/medicines", params={"keyword": keyword})
        print(f"[搜索 {keyword!r}] HTTP {resp.status_code} 返回 {len(resp.json())} 条")
        assert resp.status_code == 200, f"{keyword!r} 把接口搞崩了"

    # 表还在吗
    with Session(engine) as db:
        count = db.scalar(select(func.count()).select_from(Medicine))
    assert count >= 1, "medicine 表被搞掉了"


def test_prescription_code_is_unique(client, engine, data):
    """连开多张处方，处方号不能重复。"""
    codes = []
    for _ in range(10):
        resp = client.post("/api/prescriptions", json={
            "patient_id": data["patient"], "doctor_id": data["doctor"],
            "items": [{"medicine_id": data["medicine"], "quantity": 1}],
        })
        assert resp.status_code == 201, resp.text
        codes.append(resp.json()["code"])

    print(f"\n[连开 10 张] 处方号示例 {codes[:3]} ... 唯一数 {len(set(codes))}")
    assert len(set(codes)) == 10, f"处方号有重复：{codes}"


# ==========================================================================
# 七、外键约束与输入清洗（这两个是修完 bug 后补的回归测试）
# ==========================================================================
def test_foreign_keys_are_enabled(engine):
    """外键必须真的生效 —— 否则测试环境和生产行为不一致。

    踩过的坑：SQLite 默认 PRAGMA foreign_keys=0，外键形同虚设。
    结果是"给不存在的患者开处方"这种脏数据在测试里能写进去，
    换到 PostgreSQL 上线才报错。这条测试把开关钉住，防止再被关掉。
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        enabled = conn.execute(text("PRAGMA foreign_keys")).scalar()
    print(f"\n[外键约束] PRAGMA foreign_keys = {enabled}")
    assert enabled == 1, "外键约束没开，脏数据会被写进去"


def test_prescription_rejects_unknown_patient(engine):
    """业务层要给出明确的"患者不存在"，而不是含糊的数据库错误。"""
    with Session(engine) as db:
        with pytest.raises(pharmacy.PatientNotFound):
            pharmacy.create_prescription(
                db, patient_id=999999, doctor_id=999999,
                items=[{"medicine_id": 1, "quantity": 1}])
        db.rollback()
    print("\n[引用校验] 不存在的患者被明确拒绝 ✅")


def test_name_is_stripped_before_saving(client, engine, data):
    """患者输入常带首尾空格（手机键盘容易补空格），存库前要清掉。"""
    resp = _book(client, data["schedule"], name="  李四  ")
    print(f"\n[姓名带空格] HTTP {resp.status_code}")
    assert resp.status_code == 201

    with Session(engine) as db:
        patient = db.scalar(select(Patient).where(Patient.phone == "13800001111"))
        print(f"   存库的姓名={patient.name!r}")
        assert patient.name == "李四", "首尾空格没被清掉"


def test_symptom_is_stripped_before_triage(client):
    """症状带首尾空格时，返回的 symptom 也应是清理过的。"""
    resp = client.post("/api/triage", json={"symptom": "   咳嗽发烧   "})
    got = resp.json()["symptom"]
    print(f"\n[症状带空格] 返回的 symptom={got!r}")
    assert got == "咳嗽发烧", "首尾空格没被清掉"
