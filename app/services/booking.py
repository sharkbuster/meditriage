"""预约：下单、取消、查询。

并发抢号交给 appointment.take_slot；同一患者重复预约靠唯一索引挡住；
取消要把号还回去，但不能还得比总量多。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.models import Appointment, Department, Doctor, Patient, Schedule
from app.services import appointment as slot_ops


class AlreadyBooked(Exception):
    """同一患者已经约过这个排班。"""


class AppointmentNotFound(Exception):
    pass


class PastSchedule(Exception):
    """想约已经过去的号。"""


def book(
    db: Session,
    *,
    patient_id: int,
    schedule_id: int,
    triage_department_id: int | None = None,
    triage_confidence: float | None = None,
) -> Appointment:
    """预约一个号。

    两道防线：take_slot 的原子扣减防超卖，
    (patient_id, schedule_id) 唯一索引防同一人重复占号。

    取消后能重新预约：唯一索引管的是"这一对只留一条记录"，
    所以重约时复用那条已取消的记录。插新行会撞索引，
    结果就是取消过一次之后再也约不上。
    """
    # 过去的号要挡掉。页面上只列未来几天的号，但直接调接口能传任意 id，
    # 真约出一条"昨天上午"的记录，那就是脏数据。
    schedule = slot_ops.get_schedule(db, schedule_id)
    if schedule.work_date < date.today():
        raise PastSchedule(f"{schedule.work_date} 已经过去了，不能预约")

    existing = db.scalar(
        select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.schedule_id == schedule_id,
        )
    )
    if existing is not None and existing.status == "booked":
        raise AlreadyBooked("您已预约该时段，请勿重复操作")

    remaining = slot_ops.take_slot(db, schedule_id)   # 可能抛 NoSlotLeft / ScheduleNotFound

    if existing is not None:
        # 复用已取消的记录：重置状态即可，号源已经重新扣过一次
        existing.status = "booked"
        existing.cancelled_at = None
        existing.triage_department_id = triage_department_id
        existing.triage_confidence = triage_confidence
        appt = existing
    else:
        appt = Appointment(
            patient_id=patient_id,
            schedule_id=schedule_id,
            status="booked",
            triage_department_id=triage_department_id,
            triage_confidence=triage_confidence,
        )
        db.add(appt)

    try:
        db.flush()
    except IntegrityError as exc:
        # 并发下同一患者两个请求同时走到这里，唯一索引挡住了第二个。
        # 整个事务回滚，刚才扣掉的号源自然回到扣减前，不需要手工补偿。
        db.rollback()
        raise AlreadyBooked("您已预约该时段，请勿重复操作") from exc

    db.commit()
    db.refresh(appt)
    return appt


def cancel(db: Session, appointment_id: int) -> Appointment:
    """取消预约，号还回号池。重复取消直接返回，不重复还号。"""
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise AppointmentNotFound(f"预约单 {appointment_id} 不存在")
    if appt.status != "booked":
        return appt

    appt.status = "cancelled"
    appt.cancelled_at = datetime.now()
    slot_ops.release_slot(db, appt.schedule_id)
    db.commit()
    db.refresh(appt)
    return appt


def find_or_create_patient(db: Session, *, name: str, phone: str) -> int:
    """按手机号找人，没有就建档。

    id_card 是拿手机号占位的：患者端只收姓名和手机号，而 id_card 非空且唯一。
    真实医院当然要实名认证，这是简化，README 的已知问题里记了。
    """
    patient = db.scalar(select(Patient).where(Patient.phone == phone))
    if patient is not None:
        # 老患者可能改了名字，顺手更新
        if name and patient.name != name:
            patient.name = name
            db.flush()
        return patient.id

    patient = Patient(name=name, phone=phone, id_card=f"PENDING-{phone}")
    db.add(patient)
    db.flush()
    return patient.id


def book_for_patient(
    db: Session,
    *,
    name: str,
    phone: str,
    schedule_id: int,
    triage_department_id: int | None = None,
    triage_confidence: float | None = None,
) -> Appointment:
    """患者端入口：只给姓名 + 手机号，后端负责找人/建档。"""
    patient_id = find_or_create_patient(db, name=name, phone=phone)
    return book(
        db,
        patient_id=patient_id,
        schedule_id=schedule_id,
        triage_department_id=triage_department_id,
        triage_confidence=triage_confidence,
    )


def list_by_phone(db: Session, phone: str) -> list[Appointment]:
    """患者端「我的预约」。"""
    patient = db.scalar(select(Patient).where(Patient.phone == phone))
    if patient is None:
        return []
    return list_by_patient(db, patient.id)


def describe(db: Session, appointment: Appointment) -> dict:
    """把预约单摊平成患者看得懂的信息（医生、科室、时间）。"""
    schedule = db.get(Schedule, appointment.schedule_id)
    doctor = db.get(Doctor, schedule.doctor_id) if schedule else None
    department = db.get(Department, doctor.department_id) if doctor else None
    return {
        "id": appointment.id,
        "patient_id": appointment.patient_id,
        "schedule_id": appointment.schedule_id,
        "status": appointment.status,
        "triage_department_id": appointment.triage_department_id,
        "triage_confidence": appointment.triage_confidence,
        "created_at": appointment.created_at,
        "doctor_name": doctor.name if doctor else "",
        "doctor_title": doctor.title if doctor else "",
        "department_name": department.name if department else "",
        "work_date": schedule.work_date if schedule else None,
        "period": schedule.period if schedule else "",
        "start_time": schedule.start_time if schedule else None,
        "end_time": schedule.end_time if schedule else None,
    }


def list_by_patient(db: Session, patient_id: int) -> list[Appointment]:
    return list(
        db.scalars(
            select(Appointment)
            .where(Appointment.patient_id == patient_id)
            .order_by(Appointment.created_at.desc())
        )
    )
