"""号源并发安全测试 —— 这个项目的核心证据。

跑法：
    .venv\\Scripts\\python.exe -m pytest tests/test_concurrency.py -v -s
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import Base, enable_sqlite_pragmas
from app.domain.models import Appointment, Department, Doctor, Patient, Schedule
from app.services import appointment as slot_ops
from app.services import booking

# 并发规模刻意压小。原因见下面 fixture 的说明：
# SQLite 只有一个写者，线程开多了不是"更严格的测试"，而是"更多的等锁"。
WORKERS = 8
BUSY_TIMEOUT_MS = 5000


@pytest.fixture()
def engine(tmp_path):
    """每个测试一个独立的 SQLite 文件库，每个线程各用一个连接。

    为什么不追求大并发：
      SQLite 同一时刻只允许一个写事务，并发写会串行排队。
      线程数开大（几十上百）时，后到的线程大量撞 busy_timeout，
      测试就从"验证不超卖"退化成"验证等锁"，还很慢。
      所以这里用 8 个线程把竞争真实地制造出来，验证逻辑正确性；
      真正要出"1000 并发不超卖"这种数据，请用 PostgreSQL 压测（见 README 的压测章节）。

    用 NullPool：每个会话独占连接、用完即关，不留下长期占着读事务的连接，
    避免 WAL 下写者被读事务饿死。
    """
    db_path = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db_path}", future=True, poolclass=NullPool)

    @event.listens_for(eng, "connect")
    def _set_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        enable_sqlite_pragmas(dbapi_conn)
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _prepare(engine, *, capacity: int) -> int:
    """建一个科室/医生/排班，返回 schedule_id。"""
    with Session(engine) as db:
        dep = Department(name="测试科", keywords="测试", description="测试")
        db.add(dep)
        db.flush()
        doc = Doctor(name="测试医生", title="主治医师", department_id=dep.id)
        db.add(doc)
        db.flush()
        sch = Schedule(
            doctor_id=doc.id,
            work_date=date.today() + timedelta(days=1),
            period="上午",
            start_time=time(8, 30),
            end_time=time(11, 30),
            capacity=capacity,
            remaining=capacity,
        )
        db.add(sch)
        db.commit()
        return sch.id


def _make_patients(engine, n: int) -> list[int]:
    with Session(engine) as db:
        ids = []
        for i in range(n):
            p = Patient(name=f"患者{i}", phone=f"1390000{i:04d}", id_card=f"46010020040101{i:04d}")
            db.add(p)
            db.flush()
            ids.append(p.id)
        db.commit()
        return ids


def _booked_count(engine, schedule_id: int) -> int:
    with Session(engine) as db:
        return len(list(db.scalars(
            select(Appointment).where(Appointment.schedule_id == schedule_id)
        )))


def _remaining(engine, schedule_id: int) -> int:
    with Session(engine) as db:
        return db.get(Schedule, schedule_id).remaining


# --------------------------------------------------------------------------
# 1. 先查再改为什么错：用两个独立事务确定性地复现，不依赖并发时序
# --------------------------------------------------------------------------
def test_check_then_act_oversells(engine):
    """确定性复现超卖：两个请求先后"读"到同一份库存，然后各自"写"。

    这是 check-then-act 的本质缺陷，不需要真的并发就能证明：
    两次读之间没有锁，两次写之间也没有条件判断，库存被扣了两次而总数只有 1。
    """
    capacity = 1
    schedule_id = _prepare(engine, capacity=capacity)
    p1, p2 = _make_patients(engine, 2)

    # 请求 A 读到 stock=1，还没写
    with Session(engine) as db_a:
        seen_by_a = db_a.get(Schedule, schedule_id).remaining
        assert seen_by_a == 1

        # 请求 B 也读到 stock=1（因为 A 还没提交任何改动）
        with Session(engine) as db_b:
            seen_by_b = db_b.get(Schedule, schedule_id).remaining
            assert seen_by_b == 1, "B 应看到同一份旧库存，这正是竞态窗口"

            # B 先扣：库存 1 -> 0
            slot_ops.take_slot_naive(db_b, schedule_id)
            db_b.add(Appointment(patient_id=p2, schedule_id=schedule_id, status="booked"))
            db_b.commit()

        # A 拿着过期的 seen_by_a=1 继续扣：库存 0 -> -1
        db_a.execute(
            Schedule.__table__.update()
            .where(Schedule.__table__.c.id == schedule_id)
            .values(remaining=seen_by_a - 1)
        )
        db_a.add(Appointment(patient_id=p1, schedule_id=schedule_id, status="booked"))
        db_a.commit()

    booked = _booked_count(engine, schedule_id)
    remaining = _remaining(engine, schedule_id)
    print(f"\n[check-then-act] 号源 {capacity}，落库预约 {booked}，剩余 {remaining}")
    # 关键证据：只有 1 个号源，却产生了 2 条预约记录
    assert booked == 2 > capacity, "两个请求都成功了 —— 号源只有 1 个，发生超卖"
    # 库存被扣到 0（而不是保持 1），说明两次扣减都生效了
    assert remaining == 0, f"两次扣减都生效，剩余应为 0，实际 {remaining}"


# --------------------------------------------------------------------------
# 2. 原子扣减：并发下不多卖一个
# --------------------------------------------------------------------------
def test_no_oversell_with_atomic_update(engine):
    """原子扣减：40 个号源、40 个患者同时抢，必须一个不多一个不少地卖光。"""
    capacity = 40
    schedule_id = _prepare(engine, capacity=capacity)
    patient_ids = _make_patients(engine, capacity)

    success = [0]
    sold_out = [0]
    lock = threading.Lock()
    start = threading.Event()

    def worker(pid: int) -> None:
        start.wait()                                   # 尽量同时起跑
        with Session(engine) as db:
            try:
                booking.book(db, patient_id=pid, schedule_id=schedule_id)
                with lock:
                    success[0] += 1
            except slot_ops.NoSlotLeft:
                with lock:
                    sold_out[0] += 1
            except Exception as exc:                   # noqa: BLE001
                db.rollback()
                print(f"  意外异常 {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, pid) for pid in patient_ids]
        start.set()
        for f in futures:
            f.result()

    booked = _booked_count(engine, schedule_id)
    remaining = _remaining(engine, schedule_id)
    print(f"\n[atomic] 号源 {capacity}，成功 {success[0]}，售罄 {sold_out[0]}，"
          f"落库 {booked}，剩余 {remaining}")
    assert booked == capacity, f"应恰好卖光 {capacity} 个，实际 {booked}"
    assert remaining == 0, f"号源应放完，实际剩余 {remaining}"
    assert remaining >= 0, "原子扣减下库存永远不会变成负数"


def test_no_oversell_when_many_compete_for_few(engine):
    """号源很少、抢的人很多：只有号源数量的人能成功，其余全部收到"已放完"。"""
    capacity = 5
    schedule_id = _prepare(engine, capacity=capacity)
    patient_ids = _make_patients(engine, 30)

    success = [0]
    sold_out = [0]
    lock = threading.Lock()
    start = threading.Event()

    def worker(pid: int) -> None:
        start.wait()
        with Session(engine) as db:
            try:
                booking.book(db, patient_id=pid, schedule_id=schedule_id)
                with lock:
                    success[0] += 1
            except slot_ops.NoSlotLeft:
                with lock:
                    sold_out[0] += 1
            except Exception:                          # noqa: BLE001
                db.rollback()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, pid) for pid in patient_ids]
        start.set()
        for f in futures:
            f.result()

    booked = _booked_count(engine, schedule_id)
    print(f"\n[抢光] 号源 {capacity}，30 人抢：成功 {success[0]}，"
          f"被拒 {sold_out[0]}，落库 {booked}，剩余 {_remaining(engine, schedule_id)}")
    assert success[0] == capacity == booked, f"应恰好 {capacity} 人成功，实际 {success[0]}"


# --------------------------------------------------------------------------
# 3. 幂等：同一患者不能重复占号
# --------------------------------------------------------------------------
def test_duplicate_booking_is_rejected(engine):
    """同一患者对同一排班重复预约，只允许成功一次。"""
    schedule_id = _prepare(engine, capacity=5)
    pid = _make_patients(engine, 1)[0]

    with Session(engine) as db:
        booking.book(db, patient_id=pid, schedule_id=schedule_id)

    with Session(engine) as db:
        with pytest.raises(booking.AlreadyBooked):
            booking.book(db, patient_id=pid, schedule_id=schedule_id)

    assert _remaining(engine, schedule_id) == 4, "重复预约不应额外扣号源"
    assert _booked_count(engine, schedule_id) == 1


def test_same_patient_racing_itself(engine):
    """同一患者的 8 个并发请求同时提交：只能成功 1 次，号源只减 1。"""
    schedule_id = _prepare(engine, capacity=10)
    pid = _make_patients(engine, 1)[0]

    success = [0]
    lock = threading.Lock()
    start = threading.Event()

    def worker(_i: int) -> None:
        start.wait()
        with Session(engine) as db:
            try:
                booking.book(db, patient_id=pid, schedule_id=schedule_id)
                with lock:
                    success[0] += 1
            except Exception:                              # noqa: BLE001
                db.rollback()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, i) for i in range(WORKERS)]
        start.set()
        for f in futures:
            f.result()

    booked = _booked_count(engine, schedule_id)
    remaining = _remaining(engine, schedule_id)
    print(f"\n[幂等] 同一患者并发 {WORKERS} 次：成功 {success[0]}，落库 {booked}，剩余 {remaining}")
    assert booked == 1, f"应只有 1 条预约，实际 {booked}"
    assert remaining == 9, f"号源应只减 1，实际剩余 {remaining}"


# --------------------------------------------------------------------------
# 4. 取消要正确归还号源
# --------------------------------------------------------------------------
def test_cancel_returns_slot(engine):
    """取消后号源归还，且重复取消不会把号源越还越多。"""
    capacity = 3
    schedule_id = _prepare(engine, capacity=capacity)
    pid = _make_patients(engine, 1)[0]

    with Session(engine) as db:
        appt = booking.book(db, patient_id=pid, schedule_id=schedule_id)
        assert db.get(Schedule, schedule_id).remaining == capacity - 1

        booking.cancel(db, appt.id)
        assert db.get(Schedule, schedule_id).remaining == capacity

        booking.cancel(db, appt.id)          # 重复取消
        assert db.get(Schedule, schedule_id).remaining == capacity, "号源被重复归还"
