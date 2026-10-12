"""数据模型。

几条定下来的规矩：

号源和药品库存都是"带条件的 UPDATE 扣减"，不是先查再改。查了再改中间
有窗口，两个请求会同时读到同一份剩余量然后各扣一次。

防重复预约靠 (patient_id, schedule_id) 唯一索引，不靠应用层判断 ——
应用层那个判断同样有窗口，只用来给患者一句友好提示。

金额一律整数分。药房和收费只要沾上浮点，对账时必然出现几分钱差异。
"""
from datetime import date, datetime, time

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Department(Base):
    """科室。分诊的候选项就是这里的记录。"""

    __tablename__ = "department"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    keywords: Mapped[str] = mapped_column(String(500), default="")   # 分诊检索的口语词
    description: Mapped[str] = mapped_column(String(500), default="")
    for_child: Mapped[bool] = mapped_column(default=True)            # 是否接诊 14 岁以下

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="department")


class Doctor(Base):
    __tablename__ = "doctor"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(30), default="医师")  # 主任医师/副主任医师/主治医师
    department_id: Mapped[int] = mapped_column(ForeignKey("department.id"), index=True)
    specialty: Mapped[str] = mapped_column(String(300), default="")

    department: Mapped[Department] = relationship(back_populates="doctors")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="doctor")


class Schedule(Base):
    """排班：某医生某天某个时段放出的号源。并发抢号就抢 remaining。"""

    __tablename__ = "schedule"
    __table_args__ = (
        UniqueConstraint("doctor_id", "work_date", "period", name="uq_schedule_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctor.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    period: Mapped[str] = mapped_column(String(10))          # 上午 / 下午
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    capacity: Mapped[int] = mapped_column(Integer, default=20)
    remaining: Mapped[int] = mapped_column(Integer, default=20)

    doctor: Mapped[Doctor] = relationship(back_populates="schedules")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="schedule")


class Appointment(Base):
    """预约单。"""

    __tablename__ = "appointment"
    __table_args__ = (
        # 幂等的最后一道防线：应用层那个"约过没有"的判断有窗口
        UniqueConstraint("patient_id", "schedule_id", name="uq_appointment_patient_schedule"),
        Index("ix_appointment_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.id"), index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedule.id"), index=True)

    # booked -> cancelled / finished，取消后号源要还回去
    status: Mapped[str] = mapped_column(String(16), default="booked")
    # 分诊结果：来源科室推荐，留着做效果分析
    triage_department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_confidence: Mapped[float | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    schedule: Mapped[Schedule] = relationship(back_populates="appointments")


class Patient(Base):
    __tablename__ = "patient"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    id_card: Mapped[str] = mapped_column(String(30), unique=True)
    is_child: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ==========================================================================
# 药房与取药
# ==========================================================================
class Medicine(Base):
    """药品目录。

    price_cents 是分不是元。药品计价最怕浮点，0.1 + 0.2 那点误差
    在处方汇总和对账时会变成几分钱对不上，还特别难查。展示时除以 100。
    """

    __tablename__ = "medicine"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    spec: Mapped[str] = mapped_column(String(80), default="")       # 0.25g*24粒
    unit: Mapped[str] = mapped_column(String(10), default="盒")
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    warn_stock: Mapped[int] = mapped_column(Integer, default=10)    # 低于它就补货
    is_active: Mapped[bool] = mapped_column(default=True)


class Prescription(Base):
    """处方，一张对应一次就诊。

    pending_pay -> pending_pickup -> picked，另有 cancelled。
    只有 pending_pickup 能选取药时段，不然会出现"药还没配好就约了时间"。
    """

    __tablename__ = "prescription"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 患者报的号，形如 RX202610120004，不是主键
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.id"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment.id"), nullable=True, index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctor.id"), index=True)

    status: Mapped[str] = mapped_column(String(16), default="pending_pay", index=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)

    # 药房按批次叫号，只到时段，不排到分钟
    pickup_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    pickup_period: Mapped[str] = mapped_column(String(20), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    picked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["PrescriptionItem"]] = relationship(
        back_populates="prescription", cascade="all, delete-orphan")


class PrescriptionItem(Base):
    """处方明细：一种药一行。"""

    __tablename__ = "prescription_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(
        ForeignKey("prescription.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicine.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # 用法用量由医生填，如"口服，一次1片，一日3次"
    dosage: Mapped[str] = mapped_column(String(120), default="")
    # 下单时的单价快照。药品调价后，历史处方金额必须还能对得上。
    unit_price_cents: Mapped[int] = mapped_column(Integer, default=0)

    prescription: Mapped[Prescription] = relationship(back_populates="items")
    medicine: Mapped[Medicine] = relationship()
