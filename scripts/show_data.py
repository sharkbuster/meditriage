"""看一眼库里有什么，确认演示数据在位。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select          # noqa: E402
from sqlalchemy.orm import Session           # noqa: E402

from app.db.session import SessionLocal      # noqa: E402
from app.domain.models import (              # noqa: E402
    Appointment, Department, Doctor, Patient, Schedule,
)

with SessionLocal() as db:
    for name, model in (("科室", Department), ("医生", Doctor),
                        ("患者", Patient), ("排班", Schedule),
                        ("预约", Appointment)):
        total = db.scalar(select(func.count()).select_from(model))
        print(f"{name:<6} {total}")

    avail = db.scalar(select(func.count()).select_from(Schedule).where(Schedule.remaining > 0))
    print(f"有号的排班 {avail} 条")

    row = db.execute(
        select(Schedule.id, Doctor.name, Schedule.work_date, Schedule.period,
               Schedule.remaining, Schedule.capacity)
        .join(Doctor, Doctor.id == Schedule.doctor_id)
        .where(Schedule.remaining > 0)
        .order_by(Schedule.work_date).limit(3)
    ).all()
    print("\n可用于演示的排班（schedule_id 就是你要填的数）：")
    for r in row:
        print(f"  schedule_id={r[0]}  {r[1]}  {r[2]} {r[3]}  余号 {r[4]}/{r[5]}")

    pid = db.scalar(select(Patient.id).order_by(Patient.id).limit(1))
    print(f"\n可用于演示的 patient_id={pid}")
