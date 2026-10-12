"""药房与取药。

处方状态：pending_pay -> pending_pickup -> picked，另有 cancelled。

库存在缴费时才扣，不在开方时扣。处方开了不缴费很常见（患者改主意、
去外面药店买），开方就锁库存会把药房占死。扣减复用号源那套原子
UPDATE，理由一样：两个窗口同时发同一盒药，不能发成负数。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.models import (
    Doctor,
    Medicine,
    Patient,
    Prescription,
    PrescriptionItem,
)

# 取药时段：药房按批次叫号，不排到分钟
PICKUP_PERIODS = ("上午 08:00–12:00", "下午 14:00–17:30")
PICKUP_DAYS = 3


class MedicineNotFound(Exception):
    pass


class PatientNotFound(Exception):
    pass


class DoctorNotFound(Exception):
    pass


class OutOfStock(Exception):
    """药品库存不足。"""


class PrescriptionNotFound(Exception):
    pass


class InvalidState(Exception):
    """当前状态不允许这个操作。"""


# --------------------------------------------------------------------------
# 药品库存
# --------------------------------------------------------------------------
def take_stock(db: Session, medicine_id: int, quantity: int) -> int:
    """原子扣库存，返回扣完的剩余量。

    判断和扣减写在一条 SQL 里，跟号源一个套路：先查再扣会超发，
    药发出去就收不回来了。
    """
    if quantity <= 0:
        raise ValueError("数量必须大于 0")

    result = db.execute(
        update(Medicine)
        .where(Medicine.id == medicine_id, Medicine.stock >= quantity)
        .values(stock=Medicine.stock - quantity)
    )
    if result.rowcount == 0:
        medicine = db.get(Medicine, medicine_id)
        if medicine is None:
            raise MedicineNotFound(f"药品 {medicine_id} 不存在")
        raise OutOfStock(f"{medicine.name} 库存不足（现有 {medicine.stock}）")
    db.flush()
    return db.scalar(select(Medicine.stock).where(Medicine.id == medicine_id))


def release_stock(db: Session, medicine_id: int, quantity: int) -> None:
    """作废处方时把已扣的库存还回去。"""
    db.execute(
        update(Medicine)
        .where(Medicine.id == medicine_id)
        .values(stock=Medicine.stock + quantity)
    )
    db.flush()


def list_medicines(db: Session, keyword: str | None = None,
                   only_in_stock: bool = False) -> list[Medicine]:
    stmt = select(Medicine).where(Medicine.is_active.is_(True))
    if keyword:
        stmt = stmt.where(Medicine.name.contains(keyword))
    if only_in_stock:
        stmt = stmt.where(Medicine.stock > 0)
    return list(db.scalars(stmt.order_by(Medicine.name)))


def low_stock(db: Session) -> list[Medicine]:
    """需要补货的药。药房每天要看的列表。"""
    return list(db.scalars(
        select(Medicine)
        .where(Medicine.is_active.is_(True), Medicine.stock <= Medicine.warn_stock)
        .order_by(Medicine.stock)
    ))


# --------------------------------------------------------------------------
# 处方
# --------------------------------------------------------------------------
def _make_code(prescription_id: int, when: date | None = None) -> str:
    """开方号：RX + 开方日期 + 自增 id。

    用自增主键而不是"当天记录数+1"或"当天最大号+1"：
    后两种都要先查再写，并发时各个事务看不到彼此未提交的 INSERT，
    算出来的是同一个号，重试也还是同一个号，会一直撞。
    自增 id 由数据库分配，不冲突，也就没有重试这回事。

    序号补到 6 位。原来补 4 位，id 过万之后 "10000" 会排在 "9999" 前面
    （字符串比较），药房按号叫号就乱了。6 位够用到百万级。

    对外用这个号而不是直接露主键：RX20261012000004 比 "4" 像个单号，
    患者报号、药房找单都方便。
    """
    day = when or date.today()
    return f"RX{day:%Y%m%d}{prescription_id:06d}"


def create_prescription(
    db: Session,
    *,
    patient_id: int,
    doctor_id: int,
    appointment_id: int | None = None,
    items: list[dict],
) -> Prescription:
    """医生开方。只算金额，不动库存。

    items: [{"medicine_id": 1, "quantity": 2, "dosage": "口服，一次1片"}]

    患者和医生显式查一遍：外键虽然能挡住不存在的引用，但抛出来的是
    IntegrityError，调用方看不出到底哪里填错了。
    """
    if not items:
        raise ValueError("处方至少要有一味药")

    if db.get(Patient, patient_id) is None:
        raise PatientNotFound(f"患者 {patient_id} 不存在")
    if db.get(Doctor, doctor_id) is None:
        raise DoctorNotFound(f"医生 {doctor_id} 不存在")

    prescription = Prescription(
        code="",                                  # 先占位，拿到 id 后再补
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_id=appointment_id,
        status="pending_pay",
        total_cents=0,
    )
    db.add(prescription)
    db.flush()                                    # 这一刻拿到自增 id
    prescription.code = _make_code(prescription.id)

    total = 0
    for item in items:
        medicine = db.get(Medicine, item["medicine_id"])
        if medicine is None or not medicine.is_active:
            raise MedicineNotFound(f"药品 {item['medicine_id']} 不存在或已下架")
        quantity = int(item.get("quantity", 1))
        if quantity <= 0:
            raise ValueError("药品数量必须大于 0")

        # 金额快照：按开方这一刻的价格算，之后药品调价不影响这张处方
        line = PrescriptionItem(
            prescription_id=prescription.id,
            medicine_id=medicine.id,
            quantity=quantity,
            dosage=item.get("dosage", ""),
            unit_price_cents=medicine.price_cents,
        )
        db.add(line)
        total += medicine.price_cents * quantity

    prescription.total_cents = total
    db.commit()
    db.refresh(prescription)
    return prescription


def get(db: Session, prescription_id: int) -> Prescription:
    prescription = db.get(Prescription, prescription_id)
    if prescription is None:
        raise PrescriptionNotFound(f"处方 {prescription_id} 不存在")
    return prescription


def list_for_patient(db: Session, patient_id: int) -> list[Prescription]:
    return list(db.scalars(
        select(Prescription)
        .where(Prescription.patient_id == patient_id)
        .order_by(Prescription.created_at.desc())
    ))


def list_for_phone(db: Session, phone: str) -> list[Prescription]:
    patient = db.scalar(select(Patient).where(Patient.phone == phone))
    return [] if patient is None else list_for_patient(db, patient.id)


def pay(db: Session, prescription_id: int) -> Prescription:
    """缴费：这一刻才真正扣库存。

    整个操作在一个事务里：任何一种药扣失败，全部回滚，
    不会出现"三种药扣了两种"的半成品状态。
    """
    prescription = get(db, prescription_id)
    if prescription.status != "pending_pay":
        raise InvalidState(f"处方当前是「{STATUS_TEXT.get(prescription.status, prescription.status)}」，不能重复缴费")

    for item in prescription.items:
        take_stock(db, item.medicine_id, item.quantity)

    prescription.status = "pending_pickup"
    prescription.paid_at = datetime.now()
    db.commit()
    db.refresh(prescription)
    return prescription


def choose_pickup(db: Session, prescription_id: int,
                  pickup_date: date, period: str) -> Prescription:
    """患者选取药时段。"""
    prescription = get(db, prescription_id)
    if prescription.status != "pending_pickup":
        raise InvalidState("这张处方还没缴费，缴完费才能选取药时间")
    if period not in PICKUP_PERIODS:
        raise InvalidState("取药时段不对")
    if not (date.today() <= pickup_date <= date.today() + timedelta(days=PICKUP_DAYS)):
        raise InvalidState("只能选最近几天的取药时间")

    prescription.pickup_date = pickup_date
    prescription.pickup_period = period
    db.commit()
    db.refresh(prescription)
    return prescription


def mark_picked(db: Session, prescription_id: int) -> Prescription:
    """药房发药，患者取走。"""
    prescription = get(db, prescription_id)
    if prescription.status != "pending_pickup":
        raise InvalidState("这张处方不在待取药状态")
    prescription.status = "picked"
    prescription.picked_at = datetime.now()
    db.commit()
    db.refresh(prescription)
    return prescription


def cancel(db: Session, prescription_id: int) -> Prescription:
    """作废处方。已缴费的把库存还回去。"""
    prescription = get(db, prescription_id)
    if prescription.status == "picked":
        raise InvalidState("药已经取走了，不能作废")
    if prescription.status == "cancelled":
        return prescription

    if prescription.status == "pending_pickup":
        for item in prescription.items:
            release_stock(db, item.medicine_id, item.quantity)

    prescription.status = "cancelled"
    db.commit()
    db.refresh(prescription)
    return prescription


# --------------------------------------------------------------------------
# 删除：只允许从这里删处方
# --------------------------------------------------------------------------
def delete_prescriptions(db: Session, prescription_ids: list[int] | None = None) -> int:
    """删除处方，传 None 删全部。返回删掉的条数。

    删处方一律走这里，别在调用方自己写 delete。
    prescription_item 挂着外键，必须先删明细。这个顺序以前在 seed、
    cleanup、smoke 三个脚本里各写了一遍，三份都漏了明细，同一个
    FOREIGN KEY 报错犯了三次。
    """
    from sqlalchemy import delete

    stmt_items = delete(PrescriptionItem)
    stmt_rx = delete(Prescription)
    if prescription_ids is not None:
        if not prescription_ids:
            return 0
        stmt_items = stmt_items.where(
            PrescriptionItem.prescription_id.in_(prescription_ids))
        stmt_rx = stmt_rx.where(Prescription.id.in_(prescription_ids))

    db.execute(stmt_items)
    result = db.execute(stmt_rx)
    db.flush()
    return result.rowcount or 0


# --------------------------------------------------------------------------
# 对外展示
# --------------------------------------------------------------------------
STATUS_TEXT = {
    "pending_pay": "待缴费",
    "pending_pickup": "待取药",
    "picked": "已取药",
    "cancelled": "已作废",
}


def pickup_options() -> list[dict]:
    """可选的取药时间，供前端渲染。"""
    today = date.today()
    options = []
    for offset in range(PICKUP_DAYS + 1):
        day = today + timedelta(days=offset)
        label = {0: "今天", 1: "明天", 2: "后天"}.get(offset, f"{offset}天后")
        options.append({
            "date": day.isoformat(),
            "label": f"{label}（{day.month}月{day.day}日）",
            "periods": list(PICKUP_PERIODS),
        })
    return options


def describe(prescription: Prescription) -> dict:
    """摊平成患者看得懂的样子。金额从分转成元。"""
    return {
        "id": prescription.id,
        "code": prescription.code,
        "status": prescription.status,
        "status_text": STATUS_TEXT.get(prescription.status, prescription.status),
        "total_yuan": round(prescription.total_cents / 100, 2),
        "pickup_date": prescription.pickup_date,
        "pickup_period": prescription.pickup_period,
        "created_at": prescription.created_at,
        "items": [
            {
                "medicine_id": item.medicine_id,
                "name": item.medicine.name,
                "spec": item.medicine.spec,
                "unit": item.medicine.unit,
                "quantity": item.quantity,
                "dosage": item.dosage,
                # 单价快照，不是药品当前价
                "unit_price_yuan": round(item.unit_price_cents / 100, 2),
                "subtotal_yuan": round(item.unit_price_cents * item.quantity / 100, 2),
                "in_stock": item.medicine.stock >= item.quantity,
            }
            for item in prescription.items
        ],
    }
