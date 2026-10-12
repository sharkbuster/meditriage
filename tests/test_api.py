"""接口冒烟测试：不启动服务，直接用 TestClient 打一遍完整流程。

跑法：
    .venv\\Scripts\\python.exe -m pytest tests/test_api.py -v -s
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import Base, enable_sqlite_pragmas, get_db
from app.domain.models import Department, Doctor, Patient, Schedule
from app.main import app


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'api.db'}", future=True, poolclass=NullPool)

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
    """把应用的数据库依赖指向测试库。"""

    def override_get_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def sample(engine):
    """一条最小可用的业务数据：1 科室 / 1 医生 / 1 排班（2 个号）。"""
    with Session(engine) as db:
        dep = Department(name="呼吸内科", keywords="咳嗽 发热 气喘", description="呼吸系统疾病")
        db.add(dep)
        db.flush()
        doc = Doctor(name="张医生", title="主任医师", department_id=dep.id)
        db.add(doc)
        db.flush()
        sch = Schedule(
            doctor_id=doc.id,
            work_date=date.today() + timedelta(days=1),
            period="上午",
            start_time=time(8, 30),
            end_time=time(11, 30),
            capacity=2,
            remaining=2,
        )
        db.add(sch)
        db.commit()
        return {"department_id": dep.id, "doctor_id": doc.id, "schedule_id": sch.id}


# ----------------------------------------------------------------------
def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_patient_home_page(client):
    """根路径应返回患者界面，而不是 JSON。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "门诊预约" in resp.text


def test_departments_and_schedules(client, sample):
    assert len(client.get("/api/departments").json()) == 1
    schedules = client.get("/api/schedules").json()
    assert len(schedules) == 1
    assert schedules[0]["remaining"] == 2
    print(f"\n[排班] {schedules[0]['work_date']} {schedules[0]['period']} "
          f"剩余 {schedules[0]['remaining']}/{schedules[0]['capacity']}")


def test_triage_returns_department(client, sample):
    """分诊：咳嗽发烧气促应指向呼吸内科。"""
    resp = client.post("/api/triage", json={"symptom": "咳嗽三天了还发烧，喘不上气"})
    assert resp.status_code == 200
    body = resp.json()
    print(f"\n[分诊] confidence={body['confidence']} need_human={body['need_human']}")
    for c in body["candidates"]:
        print(f"   {c['department_name']:<8} score={c['score']:<8} {c['reason']}")
    print(f"   建议：{body['advice']}")
    assert body["candidates"], "应至少返回一个候选科室"
    assert body["candidates"][0]["department_name"] == "呼吸内科"
    assert body["need_human"] is False
    # 协议里不应暴露内部实现细节（以前这里有个 mode 字段）
    assert "mode" not in body


def test_triage_child_prefers_pediatrics(client, sample, engine):
    """儿童患者：应把儿科提到候选里。"""
    with Session(engine) as db:
        db.add(Department(name="儿科", keywords="小孩 宝宝 儿童 小儿 发热 咳嗽",
                          description="14 岁以下儿童常见病", for_child=True))
        db.commit()

    resp = client.post("/api/triage", json={"symptom": "小孩发烧咳嗽两天", "is_child": True})
    names = [c["department_name"] for c in resp.json()["candidates"]]
    print(f"\n[儿童分诊] 候选顺序：{names}")
    assert "儿科" in names


def test_triage_vague_symptom_needs_human(client, sample):
    """主诉过于笼统时应提示转人工导诊。"""
    resp = client.post("/api/triage", json={"symptom": "不太舒服"})
    body = resp.json()
    print(f"\n[模糊主诉] confidence={body['confidence']} need_human={body['need_human']} "
          f"advice={body['advice']}")
    assert body["need_human"] is True


# ----------------------------------------------------------------------
# 患者端流程：只填姓名 + 手机号，不出现 patient_id
# ----------------------------------------------------------------------
def _book(client, schedule_id, name="王小明", phone="13900000001", **extra):
    return client.post("/api/appointments",
                       json={"name": name, "phone": phone,
                             "schedule_id": schedule_id, **extra})


def test_full_patient_flow(client, sample):
    """预约 -> 重复预约被拒 -> 查我的预约 -> 取消 -> 号源归还 -> 可重约。"""
    resp = _book(client, sample["schedule_id"])
    assert resp.status_code == 201, resp.text
    appt = resp.json()
    print(f"\n[预约] 成功，预约号 {appt['id']}，医生 {appt['doctor_name']}，"
          f"科室 {appt['department_name']}")
    # 返回值要够患者看，不能只给 id
    assert appt["doctor_name"] == "张医生"
    assert appt["department_name"] == "呼吸内科"
    assert appt["work_date"]

    resp2 = _book(client, sample["schedule_id"])
    assert resp2.status_code == 409
    print(f"[预约] 重复提交被拒：{resp2.json()['detail']}")

    remaining = client.get("/api/schedules").json()[0]["remaining"]
    assert remaining == 1, f"号源应剩余 1，实际 {remaining}"

    # 我的预约：按手机号查
    mine = client.get("/api/appointments?phone=13900000001").json()
    assert len(mine) == 1
    assert mine[0]["id"] == appt["id"]
    print(f"[我的预约] 查到 {len(mine)} 条")

    # 取消
    resp3 = client.post(f"/api/appointments/{appt['id']}/cancel")
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "cancelled"
    remaining = client.get("/api/schedules").json()[0]["remaining"]
    assert remaining == 2, f"取消后号源应回到 2，实际 {remaining}"
    print(f"[取消] 号源已归还，剩余 {remaining}")

    # 取消后应能重新预约（验证释放的号确实可用）
    resp4 = _book(client, sample["schedule_id"])
    assert resp4.status_code == 201, resp4.text
    print("[预约] 取消后可重新预约成功")


def test_same_phone_reuses_patient_record(client, sample, engine):
    """同一手机号多次预约，不应重复建患者档案。"""
    _book(client, sample["schedule_id"], name="王小明", phone="13900000001")
    with Session(engine) as db:
        cnt = db.scalar(select(func.count()).select_from(Patient))
    assert cnt == 1, f"应只有 1 条患者记录，实际 {cnt}"


def test_phone_validation(client, sample):
    """手机号格式不对要挡在入口。"""
    bad = _book(client, sample["schedule_id"], phone="139")
    assert bad.status_code == 422
    print(f"\n[校验] 手机号 '139' 被拒：{bad.status_code}")


def test_booking_sold_out(client, sample):
    """号源 2 个，约满第 3 次应返回 409。"""
    codes = [_book(client, sample["schedule_id"], name=f"患者{i}",
                   phone=f"1390000000{i}").status_code for i in range(3)]
    print(f"\n[售罄] 三次预约返回码：{codes}")
    assert codes == [201, 201, 409], f"应前两个成功第三个被拒，实际 {codes}"


def test_my_appointments_empty_for_unknown_phone(client, sample):
    """陌生手机号查不到记录时返回空列表，不该报错。"""
    resp = client.get("/api/appointments?phone=13700000000")
    assert resp.status_code == 200
    assert resp.json() == []
