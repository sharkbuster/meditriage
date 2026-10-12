"""清理测试残留：多出来的处方、测试患者、测试预约。

保留规则：每个患者只保留最早的一张处方（= seed.py 造的演示处方）。
已缴费的多余处方会把库存还回去。
用法：python -m scripts.cleanup_test_data
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, or_, select      # noqa: E402
from sqlalchemy.orm import Session                    # noqa: E402

from app.db.session import SessionLocal               # noqa: E402
from app.domain.models import Appointment, Medicine, Patient, Prescription  # noqa: E402
from app.services import appointment as slot_ops      # noqa: E402
from app.services import pharmacy                     # noqa: E402

# 测试用的手机号前缀：故意和 seed.py 里的 138000000xx 区分开
TEST_PHONE_PREFIXES = ("1390000123", "1390000000", "1370000")
# 演示处方保留的数量（seed.py 给患者甲、乙各造一张）
KEEP_PRESCRIPTIONS_PER_PATIENT = 1


def main() -> int:
    db: Session = SessionLocal()
    try:
        # ---------- 1. 测试预约与测试患者 ----------
        patients = list(db.scalars(
            select(Patient).where(or_(*[Patient.phone.startswith(p)
                                        for p in TEST_PHONE_PREFIXES]))
        ))
        if patients:
            pids = [p.id for p in patients]
            appts = list(db.scalars(
                select(Appointment).where(Appointment.patient_id.in_(pids))))
            for appt in appts:
                if appt.status == "booked":
                    slot_ops.release_slot(db, appt.schedule_id)
            # 顺序不能反：处方和预约都挂在 patient 上，先删患者会撞外键
            extra_rx = list(db.scalars(
                select(Prescription).where(Prescription.patient_id.in_(pids))))
            if extra_rx:
                pharmacy.delete_prescriptions(db, [rx.id for rx in extra_rx])
                print(f"  顺带删除这些患者的处方 {len(extra_rx)} 张")
            db.execute(delete(Appointment).where(Appointment.patient_id.in_(pids)))
            db.execute(delete(Patient).where(Patient.id.in_(pids)))
            db.commit()
            print(f"清理测试患者 {len(pids)} 条、测试预约 {len(appts)} 条")
        else:
            print("没有测试患者需要清理")

        # ---------- 2. 多余的处方 ----------
        rows = db.execute(
            select(Prescription.patient_id, func.count())
            .group_by(Prescription.patient_id)
        ).all()
        removed = 0
        for patient_id, count in rows:
            if count <= KEEP_PRESCRIPTIONS_PER_PATIENT:
                continue
            # 多出来的按 id 倒序删（保留最早那张 = seed 造的）
            extra = list(db.scalars(
                select(Prescription)
                .where(Prescription.patient_id == patient_id)
                .order_by(Prescription.id.desc())
                .offset(KEEP_PRESCRIPTIONS_PER_PATIENT)
            ))
            # 已缴费的要先把库存还回去，否则库存被测试吃掉
            restock: dict[int, int] = defaultdict(int)
            for rx in extra:
                if rx.status in ("pending_pickup", "picked"):
                    for item in rx.items:
                        restock[item.medicine_id] += item.quantity
            for med_id, qty in restock.items():
                med = db.get(Medicine, med_id)
                if med:
                    med.stock += qty
                    print(f"  归还库存：{med.name} +{qty}")
            # 删除走 service 层的统一入口：它负责"先明细后主表"的顺序，
            # 调用方不需要（也不应该）知道这个细节
            pharmacy.delete_prescriptions(db, [rx.id for rx in extra])
            db.commit()
            removed += len(extra)
        print(f"清理多余处方 {removed} 张")

        left = db.scalar(select(func.count()).select_from(Prescription))
        print(f"剩余处方 {left} 张（预期 = 演示处方数）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
