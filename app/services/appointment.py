"""排班与号源的原子操作。

单独放一个文件，因为扣号源是整个系统最要紧的一处，
顺手把错误写法 take_slot_naive 也留在里面，测试要靠它复现超卖。
"""
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.models import Schedule


class NoSlotLeft(Exception):
    """号源已放完。"""


class ScheduleNotFound(Exception):
    pass


def get_schedule(db: Session, schedule_id: int) -> Schedule:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise ScheduleNotFound(f"排班 {schedule_id} 不存在")
    return schedule


def list_schedules(db: Session, doctor_id: int | None = None,
                   work_date: date | None = None) -> list[Schedule]:
    stmt = select(Schedule)
    if doctor_id is not None:
        stmt = stmt.where(Schedule.doctor_id == doctor_id)
    if work_date is not None:
        stmt = stmt.where(Schedule.work_date == work_date)
    return list(db.scalars(stmt.order_by(Schedule.work_date, Schedule.period)))


def take_slot(db: Session, schedule_id: int) -> int:
    """扣一个号源，返回扣完的剩余量。

    判断和扣减写在同一条 SQL 里，靠数据库行锁保证原子性。
    拆成"先 select 再 update"两个线程就能同时读到 remaining=1 各扣一次，
    号源被超卖。用影响行数判断有没有抢到。
    """
    stmt = (
        update(Schedule)
        .where(Schedule.id == schedule_id, Schedule.remaining > 0)
        .values(remaining=Schedule.remaining - 1)
    )
    result = db.execute(stmt)
    if result.rowcount == 0:
        # 没抢到，分两种情况，前端提示不一样
        exists = db.scalar(select(Schedule.id).where(Schedule.id == schedule_id))
        if exists is None:
            raise ScheduleNotFound(f"排班 {schedule_id} 不存在")
        raise NoSlotLeft(f"排班 {schedule_id} 号源已放完")
    db.flush()
    return db.scalar(select(Schedule.remaining).where(Schedule.id == schedule_id))


def release_slot(db: Session, schedule_id: int) -> int:
    """取消预约时还号。

    带上 remaining < capacity 这个条件：不加的话重复取消会把号源越还越多。
    """
    stmt = (
        update(Schedule)
        .where(Schedule.id == schedule_id, Schedule.remaining < Schedule.capacity)
        .values(remaining=Schedule.remaining + 1)
    )
    db.execute(stmt)
    db.flush()
    return db.scalar(select(Schedule.remaining).where(Schedule.id == schedule_id))


def take_slot_naive(db: Session, schedule_id: int) -> int:
    """先查再改的错误写法，测试用它复现超卖，线上别用。"""
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise ScheduleNotFound(f"排班 {schedule_id} 不存在")
    if schedule.remaining <= 0:
        raise NoSlotLeft(f"排班 {schedule_id} 号源已放完")
    schedule.remaining = schedule.remaining - 1
    db.flush()
    return schedule.remaining
