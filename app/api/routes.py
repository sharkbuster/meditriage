"""HTTP 接口层。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.models import Department, Doctor, Medicine, Schedule
from app.domain.schemas import (
    AppointmentCreate,
    AppointmentDetail,
    AppointmentOut,
    DepartmentOut,
    DoctorOut,
    MedicineOut,
    PickupChoose,
    PickupOption,
    PrescriptionCreate,
    PrescriptionOut,
    ScheduleCreate,
    ScheduleOut,
    TriageRequest,
    TriageResponse,
)
from app.services import booking, pharmacy
from app.services import appointment as slot_ops
from app.services.triage import TriageService

router = APIRouter(prefix="/api")
_triage_service: TriageService | None = None


def get_triage_service() -> TriageService:
    """分诊索引构建有一定开销（要遍历科室语料），进程内复用一份。"""
    global _triage_service
    if _triage_service is None:
        _triage_service = TriageService()
    return _triage_service


@router.get("/health", tags=["系统"])
def health() -> dict:
    return {"status": "ok"}


@router.get("/departments", response_model=list[DepartmentOut], tags=["科室"])
def list_departments(db: Session = Depends(get_db)) -> list[Department]:
    return list(db.scalars(select(Department).order_by(Department.id)))


@router.get("/doctors", response_model=list[DoctorOut], tags=["医生"])
def list_doctors(
    department_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Doctor]:
    stmt = select(Doctor)
    if department_id is not None:
        stmt = stmt.where(Doctor.department_id == department_id)
    return list(db.scalars(stmt.order_by(Doctor.id)))


@router.get("/schedules", response_model=list[ScheduleOut], tags=["排班"])
def list_schedules(
    doctor_id: int | None = Query(default=None),
    work_date: date | None = Query(default=None),
    only_available: bool = Query(default=False, description="只看还有号的"),
    db: Session = Depends(get_db),
) -> list[Schedule]:
    items = slot_ops.list_schedules(db, doctor_id=doctor_id, work_date=work_date)
    if only_available:
        items = [s for s in items if s.remaining > 0]
    return items


@router.post("/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED,
             tags=["排班"])
def create_schedule(payload: ScheduleCreate, db: Session = Depends(get_db)) -> Schedule:
    """管理员放号。"""
    if db.get(Doctor, payload.doctor_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "医生不存在")

    existed = db.scalar(
        select(Schedule).where(
            Schedule.doctor_id == payload.doctor_id,
            Schedule.work_date == payload.work_date,
            Schedule.period == payload.period,
        )
    )
    if existed is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该医生此时段已有排班")

    schedule = Schedule(
        doctor_id=payload.doctor_id,
        work_date=payload.work_date,
        period=payload.period,
        start_time=payload.start_time,
        end_time=payload.end_time,
        capacity=payload.capacity,
        remaining=payload.capacity,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.post("/triage", response_model=TriageResponse, tags=["分诊"])
def triage(payload: TriageRequest, service: TriageService = Depends(get_triage_service)) -> dict:
    """症状分诊：返回候选科室、置信度和就医建议。"""
    return service.triage(payload.symptom, is_child=payload.is_child, top_k=payload.top_k)


@router.post("/appointments", response_model=AppointmentDetail,
             status_code=status.HTTP_201_CREATED, tags=["预约"],
             summary="提交预约（患者端）")
def create_appointment(payload: AppointmentCreate, db: Session = Depends(get_db)) -> dict:
    """患者只需填姓名和手机号。后端按手机号找人，找不到就建档。"""
    try:
        appt = booking.book_for_patient(
            db,
            name=payload.name,
            phone=payload.phone,
            schedule_id=payload.schedule_id,
            triage_department_id=payload.triage_department_id,
            triage_confidence=payload.triage_confidence,
        )
    except slot_ops.NoSlotLeft as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "这个号已经被约完了，请换一个时间") from exc
    except slot_ops.ScheduleNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这个排班") from exc
    except booking.PastSchedule as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except booking.AlreadyBooked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "您已经约过这个时间了，不用重复预约") from exc
    return booking.describe(db, appt)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentDetail,
             tags=["预约"], summary="取消预约")
def cancel_appointment(appointment_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        appt = booking.cancel(db, appointment_id)
    except booking.AppointmentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这条预约") from exc
    return booking.describe(db, appt)


@router.get("/appointments", response_model=list[AppointmentDetail], tags=["预约"],
            summary="查我的预约（按手机号）")
def list_my_appointments(phone: str = Query(pattern=r"^1[3-9]\d{9}$"),
                         db: Session = Depends(get_db)) -> list[dict]:
    """患者端「我的预约」：输手机号就能看，不需要注册登录。"""
    return [booking.describe(db, a) for a in booking.list_by_phone(db, phone)]


# ==========================================================================
# 药房与取药
# ==========================================================================
def _medicine_out(m: Medicine) -> MedicineOut:
    return MedicineOut(
        id=m.id, name=m.name, spec=m.spec, unit=m.unit,
        manufacturer=m.manufacturer,
        price_yuan=round(m.price_cents / 100, 2),
        stock=m.stock,
        low=m.stock <= m.warn_stock,
    )


@router.get("/medicines", response_model=list[MedicineOut], tags=["药房"],
            summary="药品目录")
def list_medicines(
    keyword: str | None = Query(default=None, max_length=40),
    only_in_stock: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[MedicineOut]:
    return [_medicine_out(m) for m in pharmacy.list_medicines(db, keyword, only_in_stock)]


@router.get("/medicines/low-stock", response_model=list[MedicineOut], tags=["药房"],
            summary="需要补货的药品")
def low_stock_medicines(db: Session = Depends(get_db)) -> list[MedicineOut]:
    """给药房用的：库存低于警戒线的药。"""
    return [_medicine_out(m) for m in pharmacy.low_stock(db)]


@router.get("/pickup-options", response_model=list[PickupOption], tags=["药房"],
            summary="可选的取药时间")
def pickup_options() -> list[dict]:
    return pharmacy.pickup_options()


@router.post("/prescriptions", response_model=PrescriptionOut,
             status_code=status.HTTP_201_CREATED, tags=["药房"], summary="开处方")
def create_prescription(payload: PrescriptionCreate, db: Session = Depends(get_db)) -> dict:
    """医生开方。只算金额不动库存，库存在缴费时才扣。"""
    try:
        prescription = pharmacy.create_prescription(
            db,
            patient_id=payload.patient_id,
            doctor_id=payload.doctor_id,
            appointment_id=payload.appointment_id,
            items=[i.model_dump() for i in payload.items],
        )
    except (pharmacy.MedicineNotFound, pharmacy.PatientNotFound,
            pharmacy.DoctorNotFound) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return pharmacy.describe(prescription)


@router.post("/prescriptions/{prescription_id}/pay", response_model=PrescriptionOut,
             tags=["药房"], summary="处方缴费")
def pay_prescription(prescription_id: int, db: Session = Depends(get_db)) -> dict:
    """缴费时才真正扣药品库存。库存不足会整单失败并回滚。"""
    try:
        prescription = pharmacy.pay(db, prescription_id)
    except pharmacy.PrescriptionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except pharmacy.OutOfStock as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{exc}，请联系药房") from exc
    except pharmacy.InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return pharmacy.describe(prescription)


@router.post("/prescriptions/{prescription_id}/pickup", response_model=PrescriptionOut,
             tags=["药房"], summary="选取药时间")
def choose_pickup(prescription_id: int, payload: PickupChoose,
                  db: Session = Depends(get_db)) -> dict:
    try:
        prescription = pharmacy.choose_pickup(
            db, prescription_id, payload.pickup_date, payload.period)
    except pharmacy.PrescriptionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except pharmacy.InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return pharmacy.describe(prescription)


@router.post("/prescriptions/{prescription_id}/picked", response_model=PrescriptionOut,
             tags=["药房"], summary="药房发药（标记已取走）")
def mark_picked(prescription_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        prescription = pharmacy.mark_picked(db, prescription_id)
    except pharmacy.PrescriptionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except pharmacy.InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return pharmacy.describe(prescription)


@router.post("/prescriptions/{prescription_id}/cancel", response_model=PrescriptionOut,
             tags=["药房"], summary="作废处方")
def cancel_prescription(prescription_id: int, db: Session = Depends(get_db)) -> dict:
    """已缴费的处方作废时会把库存还回去。"""
    try:
        prescription = pharmacy.cancel(db, prescription_id)
    except pharmacy.PrescriptionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except pharmacy.InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return pharmacy.describe(prescription)


@router.get("/prescriptions", response_model=list[PrescriptionOut], tags=["药房"],
            summary="查我的处方（按手机号）")
def list_my_prescriptions(phone: str = Query(pattern=r"^1[3-9]\d{9}$"),
                          db: Session = Depends(get_db)) -> list[dict]:
    """患者端「取药」：输手机号看自己的处方和取药时间。"""
    return [pharmacy.describe(p) for p in pharmacy.list_for_phone(db, phone)]


@router.get("/patients/{patient_id}/appointments", response_model=list[AppointmentOut],
            tags=["预约"])
def list_patient_appointments(patient_id: int, db: Session = Depends(get_db)) -> list[AppointmentOut]:
    return [AppointmentOut.model_validate(a) for a in booking.list_by_patient(db, patient_id)]
