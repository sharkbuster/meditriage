"""生成层（llm.py）与业务边界的测试。

llm.py 原来一条测试都没有。三种模式里 local/api 要联网，所以用假的
httpx.post 替掉，专门验证"模型不听话的时候会怎样"：
返回空内容、返回错误结构、超时、key 没配。
接口返回不合预期时不能让整站 500，也不能把空字符串当建议发给患者。
"""
from __future__ import annotations

import datetime as dt
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.session import Base, enable_sqlite_pragmas, get_db
from app.domain.models import Department, Doctor, Medicine, Patient, Schedule
from app.main import app
from app.services import llm, pharmacy

SYMPTOM = "咳嗽三天了还发烧"
DEPTS = ["呼吸内科", "儿科"]


def _settings(mode: str, **kw) -> Settings:
    base = dict(triage_mode=mode, api_key="", ollama_base_url="http://fake",
                api_base_url="http://fake", ollama_model="m", api_model="m")
    base.update(kw)
    return Settings(**base)          # type: ignore[arg-type]


# ==========================================================================
# 一、生成层：模型不听话的时候
# ==========================================================================
def test_rule_mode_never_calls_network(monkeypatch):
    """rule 模式不该发任何网络请求。"""
    def boom(*a, **kw):
        raise AssertionError("rule 模式不该调用 httpx.post")

    monkeypatch.setattr(llm.httpx, "post", boom)
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("rule"))
    print(f"\n[rule] {advice}")
    assert "呼吸内科" in advice


def test_build_advice_with_no_departments():
    """没有候选科室时兜底话术不能崩，也不能编一个科室出来。"""
    text = llm.build_advice([])
    print(f"\n[无候选] {text}")
    assert "导诊台" in text


def test_local_mode_parses_ollama_response(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "  建议先去呼吸内科看看  "}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeResp())
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("local"))
    print(f"\n[local 正常] {advice!r}")
    assert advice == "建议先去呼吸内科看看", "首尾空白没清掉"


def test_local_mode_empty_response(monkeypatch):
    """模型返回空内容 —— 这是最容易漏的一条路径。"""
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": ""}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeResp())
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("local"))
    print(f"\n[local 空响应] 返回 {advice!r}")
    assert advice == "", "当前行为是原样返回空串，调用方必须自己兜住"


def test_local_mode_missing_field(monkeypatch):
    """响应里没有 response 字段：现在是取默认值返回空串，不该抛异常。"""
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"done": True}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeResp())
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("local"))
    print(f"\n[local 缺字段] 返回 {advice!r}")
    assert advice == ""


def test_local_mode_connection_error(monkeypatch):
    """Ollama 没启动 —— 连接被拒。异常要抛上来给调用方兜，不能吞掉。"""
    def refuse(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm.httpx, "post", refuse)
    with pytest.raises(httpx.ConnectError):
        llm.generate_advice(SYMPTOM, DEPTS, _settings("local"))
    print("\n[local 连不上] 异常正常抛出 ✅")


def test_api_mode_requires_key():
    """api 模式没配 key，要给出明确提示而不是拿空 key 去请求。"""
    with pytest.raises(RuntimeError, match="APP_API_KEY"):
        llm.generate_advice(SYMPTOM, DEPTS, _settings("api", api_key=""))
    print("\n[api 缺 key] 明确报错 ✅")


def test_api_mode_parses_openai_response(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "  去呼吸内科  "}}]}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeResp())
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("api", api_key="sk-x"))
    print(f"\n[api 正常] {advice!r}")
    assert advice == "去呼吸内科"


@pytest.mark.parametrize("payload", [
    {},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": {}}]},
    {"error": {"message": "rate limited"}},
])
def test_api_mode_malformed_response(monkeypatch, payload):
    """代理或模型返回非预期结构。

    现在是硬索引，会抛 KeyError / IndexError。
    调用方有 try/except 兜住，所以不影响分诊，但错误信息不好看。
    这条测试把当前行为记录下来。
    """
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeResp())
    with pytest.raises((KeyError, IndexError, TypeError)):
        llm.generate_advice(SYMPTOM, DEPTS, _settings("api", api_key="sk-x"))
    print(f"\n[api 结构异常] {payload} -> 抛出异常（由调用方兜底）")


def test_unknown_mode_falls_back_to_rule():
    """配置写错成 triage_mode=xxx 时应该退回规则话术，而不是崩。"""
    advice = llm.generate_advice(SYMPTOM, DEPTS, _settings("typo_mode"))
    print(f"\n[未知模式] 退回规则话术：{advice}")
    assert "呼吸内科" in advice


def test_prompt_survives_braces_in_symptom(monkeypatch):
    """主诉里带大括号不能让 format 崩掉。"""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "ok"}

    def capture(url, **kw):
        captured.update(kw.get("json", {}))
        return FakeResp()

    monkeypatch.setattr(llm.httpx, "post", capture)
    llm.generate_advice("发烧 {name} {0} {}", DEPTS, _settings("local"))
    print(f"\n[大括号] prompt 里的主诉：{captured['prompt'][:60]}")
    assert "{name}" in captured["prompt"], "主诉内容被 format 吃掉了"


# ==========================================================================
# 二、分诊在异常配置下的行为
# ==========================================================================
def test_triage_falls_back_when_model_returns_empty(monkeypatch):
    """模型没报错但返回空内容时，患者不能看到一个空的建议框。

    这是修过的一个 bug：原来只在"抛异常"时回退，返回空串不算异常，
    advice 就直接是空的。代理拦截、模型抽风、响应结构变了都会走到这条路。
    """
    from app.services import triage as triage_mod

    class EmptyResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "   "}          # 模型返回了内容，但只有空白

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: EmptyResp())
    svc = triage_mod.TriageService()
    svc.settings = _settings("local")

    result = svc.triage(SYMPTOM)
    print(f"\n[模型返回空白] advice={result['advice']!r}")
    assert result["advice"].strip(), "建议是空的，患者会看到一个空框"
    assert "呼吸内科" in result["advice"], "应该回退成规则话术"


def test_triage_still_works_when_llm_fails(monkeypatch):
    """模型挂了，分诊结果必须照常返回（这是当初把生成层拆出去的原因）。"""
    from app.services import triage as triage_mod

    def refuse(*a, **kw):
        raise httpx.ConnectError("ollama down")

    monkeypatch.setattr(llm.httpx, "post", refuse)
    svc = triage_mod.TriageService()
    svc.settings = _settings("local")           # 强制走 local

    result = svc.triage(SYMPTOM)
    print(f"\n[模型挂了] 仍返回 {len(result['candidates'])} 个候选，"
          f"建议={result['advice']!r}")
    assert result["candidates"], "模型故障不该影响分诊结果"
    assert result["candidates"][0]["department_name"] == "呼吸内科"
    assert result["advice"], "回退后建议不能是空的"


def test_triage_empty_symptom_after_strip():
    """纯空白在 schema 层就被挡，这里确认服务层也不会给出确定推荐。"""
    from app.services.triage import TriageService

    svc = TriageService()
    result = svc.triage("　　")                  # 全角空格
    print(f"\n[全角空格] need_human={result['need_human']} 候选={len(result['candidates'])}")
    assert result["need_human"] is True


# ==========================================================================
# 三、业务边界：时间、数量、跨天
# ==========================================================================
@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'extra.db'}", future=True, poolclass=NullPool)

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
        patient = Patient(name="张三", phone="13800004444", id_card="460100200401014444")
        db.add(patient)
        db.flush()
        med = Medicine(name="药甲", spec="10片", unit="盒", price_cents=500, stock=50)
        db.add(med)
        db.commit()
        return {"dep": dep.id, "doctor": doc.id, "patient": patient.id, "med": med.id}


def test_appointment_in_the_past_is_rejected(client, engine, data):
    """约昨天的号要被挡。

    这个洞是写边界测试时发现的：页面上只列未来几天的号，
    但直接调接口能传任意 schedule_id，于是能约出一条"昨天上午"的记录。
    校验放在 booking.book 里，因为那是所有预约的必经之路。
    """
    with Session(engine) as db:
        sch = Schedule(doctor_id=data["doctor"], work_date=date.today() - timedelta(days=1),
                       period="上午", start_time=dt.time(8, 30), end_time=dt.time(11, 30),
                       capacity=2, remaining=2)
        db.add(sch)
        db.commit()
        sid = sch.id

    resp = client.post("/api/appointments",
                       json={"name": "李四", "phone": "13800005555", "schedule_id": sid})
    print(f"\n[约昨天的号] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 409
    assert "过去" in resp.json()["detail"]

    # 号源不能被扣
    with Session(engine) as db:
        assert db.get(Schedule, sid).remaining == 2, "被拒的预约不该动号源"


def test_appointment_today_is_allowed(client, engine, data):
    """今天的号要允许（患者上午约下午的号很正常）。"""
    with Session(engine) as db:
        sch = Schedule(doctor_id=data["doctor"], work_date=date.today(),
                       period="下午", start_time=dt.time(14, 0), end_time=dt.time(17, 0),
                       capacity=2, remaining=2)
        db.add(sch)
        db.commit()
        sid = sch.id

    resp = client.post("/api/appointments",
                       json={"name": "王五", "phone": "13800006666", "schedule_id": sid})
    print(f"\n[约今天的号] HTTP {resp.status_code}")
    assert resp.status_code == 201


def test_schedule_capacity_upper_bound(client, data):
    """放号数量超上限要被挡（防止手滑填 99999）。"""
    payload = {
        "doctor_id": data["doctor"], "work_date": (date.today() + timedelta(days=2)).isoformat(),
        "period": "上午", "start_time": "08:30:00", "end_time": "11:30:00",
        "capacity": 99999,
    }
    resp = client.post("/api/schedules", json=payload)
    print(f"\n[放号 99999] HTTP {resp.status_code}")
    assert resp.status_code == 422


def test_schedule_capacity_zero_or_negative(client, data):
    for cap in (0, -5):
        payload = {
            "doctor_id": data["doctor"],
            "work_date": (date.today() + timedelta(days=3)).isoformat(),
            "period": "上午", "start_time": "08:30:00", "end_time": "11:30:00",
            "capacity": cap,
        }
        resp = client.post("/api/schedules", json=payload)
        print(f"[放号 {cap}] HTTP {resp.status_code}")
        assert resp.status_code == 422


def test_schedule_bad_period(client, data):
    """时段只允许上午/下午。"""
    payload = {
        "doctor_id": data["doctor"], "work_date": (date.today() + timedelta(days=4)).isoformat(),
        "period": "半夜", "start_time": "08:30:00", "end_time": "11:30:00", "capacity": 5,
    }
    resp = client.post("/api/schedules", json=payload)
    print(f"\n[时段=半夜] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 422


def test_schedule_end_before_start(client, data):
    """下班时间早于上班时间要被挡。

    这条也是边界测试发现的：原来只校验了 period 是上午/下午，
    时间先后完全没管，能放出一个"11:30 上班、08:30 下班"的号。
    """
    payload = {
        "doctor_id": data["doctor"], "work_date": (date.today() + timedelta(days=5)).isoformat(),
        "period": "上午", "start_time": "11:30:00", "end_time": "08:30:00", "capacity": 5,
    }
    resp = client.post("/api/schedules", json=payload)
    print(f"\n[时间倒置] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 422


def test_schedule_past_date_rejected(client, data):
    """给过去的日期放号要被挡。"""
    payload = {
        "doctor_id": data["doctor"], "work_date": (date.today() - timedelta(days=1)).isoformat(),
        "period": "上午", "start_time": "08:30:00", "end_time": "11:30:00", "capacity": 5,
    }
    resp = client.post("/api/schedules", json=payload)
    print(f"\n[给昨天放号] HTTP {resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 422


def test_schedule_today_is_allowed(client, data):
    """今天是允许的（当天临时加号是常见操作）。"""
    payload = {
        "doctor_id": data["doctor"], "work_date": date.today().isoformat(),
        "period": "下午", "start_time": "14:00:00", "end_time": "17:00:00", "capacity": 5,
    }
    resp = client.post("/api/schedules", json=payload)
    print(f"\n[给今天放号] HTTP {resp.status_code}")
    assert resp.status_code == 201


def test_appointment_unknown_doctor(client, data):
    resp = client.post("/api/schedules", json={
        "doctor_id": 999999, "work_date": (date.today() + timedelta(days=6)).isoformat(),
        "period": "上午", "start_time": "08:30:00", "end_time": "11:30:00", "capacity": 5})
    print(f"\n[给不存在的医生放号] HTTP {resp.status_code}")
    assert resp.status_code == 404


def test_duplicate_schedule_same_slot(client, data):
    """同一医生同一天同一时段放两次号 —— 唯一约束该挡住。"""
    payload = {
        "doctor_id": data["doctor"], "work_date": (date.today() + timedelta(days=7)).isoformat(),
        "period": "上午", "start_time": "08:30:00", "end_time": "11:30:00", "capacity": 5,
    }
    r1 = client.post("/api/schedules", json=payload)
    r2 = client.post("/api/schedules", json=payload)
    print(f"\n[重复放号] 第一次 {r1.status_code}，第二次 {r2.status_code} "
          f"detail={r2.json().get('detail')}")
    assert r1.status_code == 201
    assert r2.status_code == 409


def test_stock_cannot_go_negative_via_admin(engine, data):
    """没有直接改库存的接口，确认一下别哪天被人加上又忘了校验。"""
    from app.main import app as fastapi_app

    paths = [r.path for r in fastapi_app.routes if hasattr(r, "path")]
    risky = [p for p in paths if "stock" in p and p != "/api/medicines/low-stock"]
    print(f"\n[改库存接口] {risky or '不存在'}")
    assert not risky, "出现了能直接改库存的接口，需要补校验"


def test_prescription_total_matches_items_after_price_change(engine, data):
    """改药价后新开方的金额要按新价算，老处方不受影响。"""
    with Session(engine) as db:
        old = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["med"], "quantity": 2}])
        old_total = old.total_cents
        med = db.get(Medicine, data["med"])
        med.price_cents = 800
        db.commit()

        new = pharmacy.create_prescription(
            db, patient_id=data["patient"], doctor_id=data["doctor"],
            items=[{"medicine_id": data["med"], "quantity": 2}])
        new_total = new.total_cents
        old_after = pharmacy.get(db, old.id).total_cents

    print(f"\n[改价] 老处方 {old_total} -> {old_after}，新处方 {new_total}")
    assert old_after == old_total == 1000
    assert new_total == 1600
