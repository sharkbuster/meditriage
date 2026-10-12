"""并发压测，出 QPS 和 P95。

只认真实数据库。SQLite 同一时刻只允许一个写事务，在那里压出来的
是"排队等锁"的数字，不是系统能力，所以脚本会直接拒绝跑。

准备：
    docker compose up -d db
    set APP_DATABASE_URL=postgresql+psycopg://meditriage:meditriage@localhost:5432/meditriage
    pip install "psycopg[binary]"

跑法：
    python -m scripts.loadtest                      # 200 并发、500 号源
    python -m scripts.loadtest --concurrency 400 --capacity 1000
    python -m scripts.loadtest --compare-naive      # 顺便跑一遍错误写法对照
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text      # noqa: E402
from sqlalchemy.orm import Session                      # noqa: E402

from app.core.config import get_settings                # noqa: E402
from app.db.session import Base                         # noqa: E402
from app.domain.models import (                         # noqa: E402
    Appointment, Department, Doctor, Patient, Schedule,
)
from app.services import appointment as slot_ops        # noqa: E402
from app.services import booking                        # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def prepare(engine, capacity: int, patients: int) -> int:
    """建一套干净的压测数据，返回 schedule_id。"""
    with Session(engine) as db:
        # 压测表每次重建，避免历史数据干扰
        dep = Department(name="压测科", keywords="压测", description="压测专用")
        db.add(dep)
        db.flush()
        doc = Doctor(name="压测医生", title="主治医师", department_id=dep.id)
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
        db.flush()

        batch = []
        for i in range(patients):
            batch.append(Patient(
                name=f"压测患者{i}",
                phone=f"137{i:08d}",
                id_card=f"46010019900101{i:06d}",
            ))
        db.add_all(batch)
        db.commit()
        return sch.id


def run_load(engine, schedule_id: int, patient_ids: list[int], concurrency: int) -> dict:
    latencies: list[float] = []
    success = 0
    sold_out = 0
    errors: list[str] = []
    lock = threading.Lock()

    def worker(pid: int) -> None:
        nonlocal success, sold_out
        started = time.perf_counter()
        try:
            with Session(engine) as db:
                booking.book(db, patient_id=pid, schedule_id=schedule_id)
            with lock:
                success += 1
        except slot_ops.NoSlotLeft:
            with lock:
                sold_out += 1
        except Exception as exc:                       # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            with lock:
                latencies.append((time.perf_counter() - started) * 1000)

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, patient_ids))
    wall = time.perf_counter() - wall_start

    return {
        "requests": len(patient_ids),
        "success": success,
        "sold_out": sold_out,
        "errors": errors,
        "wall_seconds": wall,
        "qps": len(patient_ids) / wall if wall > 0 else 0.0,
        "p50": percentile(latencies, 0.50),
        "p95": percentile(latencies, 0.95),
        "p99": percentile(latencies, 0.99),
        "mean": statistics.fmean(latencies) if latencies else 0.0,
    }


def run_naive(engine, schedule_id: int, patient_ids: list[int], concurrency: int) -> dict:
    """错误写法的对照：同样压力下会超卖。"""
    def worker(pid: int) -> None:
        with Session(engine) as db:
            try:
                slot_ops.take_slot_naive(db, schedule_id)
                db.add(Appointment(patient_id=pid, schedule_id=schedule_id, status="booked"))
                db.commit()
            except Exception:                          # noqa: BLE001
                db.rollback()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, patient_ids))
    wall = time.perf_counter() - start

    with Session(engine) as db:
        sched = db.get(Schedule, schedule_id)
        booked = len(list(db.scalars(
            select(Appointment).where(Appointment.schedule_id == schedule_id)
        )))
        return {"wall_seconds": wall, "booked": booked, "remaining": sched.remaining}


def report(result: dict, capacity: int, booked: int, remaining: int) -> None:
    print()
    print("=" * 70)
    print("压测结果")
    print("=" * 70)
    print(f"  总请求数      ：{result['requests']}")
    print(f"  并发数        ：见命令行参数")
    print(f"  耗时          ：{result['wall_seconds']:.3f} s")
    print(f"  QPS           ：{result['qps']:.1f}")
    print(f"  成功预约      ：{result['success']}")
    print(f"  号源已放完    ：{result['sold_out']}")
    print(f"  异常          ：{len(result['errors'])}")
    for e in result["errors"][:5]:
        print(f"      {e}")
    print()
    print(f"  延迟 mean     ：{result['mean']:.2f} ms")
    print(f"  延迟 P50      ：{result['p50']:.2f} ms")
    print(f"  延迟 P95      ：{result['p95']:.2f} ms")
    print(f"  延迟 P99      ：{result['p99']:.2f} ms")
    print()
    print(f"  号源总数      ：{capacity}")
    print(f"  落库预约数    ：{booked}")
    print(f"  剩余号源      ：{remaining}")
    oversold = booked > capacity or remaining < 0
    print(f"  是否超卖      ：{'是 ❌' if oversold else '否 ✅'}")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="号源并发压测")
    parser.add_argument("--concurrency", type=int, default=200, help="并发线程数")
    parser.add_argument("--capacity", type=int, default=500, help="号源总数")
    parser.add_argument("--patients", type=int, default=0, help="患者数，默认等于号源数")
    parser.add_argument("--compare-naive", action="store_true", help="额外跑一遍错误写法做对照")
    args = parser.parse_args()

    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        print("当前数据库是 SQLite，压测结果没有意义（单写者，测的是等锁）。")
        print("请先起 PostgreSQL 并设置 APP_DATABASE_URL，例如：")
        print("    docker compose up -d db")
        print('    set APP_DATABASE_URL=postgresql+psycopg://meditriage:meditriage@localhost:5432/meditriage')
        return 2

    if not settings.database_url.startswith("postgresql"):
        print(f"当前 APP_DATABASE_URL 不是 PostgreSQL：{settings.database_url}")
        print("为了不给出误导性的数字，脚本到此退出。")
        return 2

    print(f"目标数据库：{settings.database_url.split('@')[-1]}")
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    patients = args.patients or args.capacity
    schedule_id = prepare(engine, args.capacity, patients)
    with Session(engine) as db:
        patient_ids = list(db.scalars(select(Patient.id).order_by(Patient.id)))

    print(f"准备完成：号源 {args.capacity}，患者 {len(patient_ids)}，"
          f"并发 {args.concurrency}")

    if args.compare_naive:
        print("\n[对照] 先跑错误写法（先查再改）...")
        naive = run_naive(engine, schedule_id, patient_ids, args.concurrency)
        print(f"  耗时 {naive['wall_seconds']:.3f}s，落库预约 {naive['booked']}，"
              f"剩余 {naive['remaining']}")
        print(f"  结论：{'超卖 ' + str(naive['booked'] - args.capacity) + ' 个' if naive['booked'] > args.capacity else '未复现超卖（压测下窗口较窄，可用测试用例做确定性复现）'}")
        # 重置数据再跑正确实现
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        schedule_id = prepare(engine, args.capacity, patients)
        with Session(engine) as db:
            patient_ids = list(db.scalars(select(Patient.id).order_by(Patient.id)))

    result = run_load(engine, schedule_id, patient_ids, args.concurrency)

    with Session(engine) as db:
        sched = db.get(Schedule, schedule_id)
        booked = len(list(db.scalars(
            select(Appointment).where(Appointment.schedule_id == schedule_id)
        )))
        remaining = sched.remaining

    report(result, args.capacity, booked, remaining)
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
