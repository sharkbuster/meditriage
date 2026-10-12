"""接口出入参。

一个坑：Pydantic 的 min_length 数字符个数，不 strip。
symptom="   " 长度是 3，照样过 min_length=1，然后一路流到业务层，
变成"患者什么都没填，系统还给他推荐了个科室"。
所以带文本的字段都挂 field_validator 先 strip 再判空。
"""
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str = ""
    for_child: bool = True


class DoctorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    title: str
    department_id: int
    specialty: str = ""


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    doctor_id: int
    work_date: date
    period: str
    start_time: time
    end_time: time
    capacity: int
    remaining: int


class ScheduleCreate(BaseModel):
    """管理员放号。"""

    doctor_id: int
    work_date: date
    period: str = Field(pattern="^(上午|下午)$")
    start_time: time
    end_time: time
    capacity: int = Field(gt=0, le=200)

    @field_validator("work_date")
    @classmethod
    def _no_past_date(cls, v: date) -> date:
        # 放过去的号没有意义，而且会让人以为可以约
        if v < date.today():
            raise ValueError("不能给过去的日期放号")
        return v

    @field_validator("end_time")
    @classmethod
    def _end_after_start(cls, v: time, info) -> time:
        start = info.data.get("start_time")
        if start is not None and v <= start:
            raise ValueError("结束时间要晚于开始时间")
        return v


class AppointmentCreate(BaseModel):
    """患者端提交预约。

    刻意不收 patient_id：那是数据库主键，患者界面上不该出现。
    患者只需要填姓名和手机号，后端用手机号找人（找不到就建一个）。
    """

    name: str = Field(min_length=1, max_length=20, description="就诊人姓名")
    phone: str = Field(pattern=r"^1[3-9]\d{9}$", description="手机号，用于取号")
    schedule_id: int
    # 可选：患者先做了分诊再挂号，把分诊结果一起带上，方便做效果分析
    triage_department_id: int | None = None
    triage_confidence: float | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("请填写就诊人姓名")
        return cleaned


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    schedule_id: int
    status: str
    triage_department_id: int | None = None
    triage_confidence: float | None = None
    created_at: datetime | None = None


class AppointmentDetail(AppointmentOut):
    """给患者看的预约详情：把医生、科室、时间都摊平，免得前端再查三次。"""

    doctor_name: str = ""
    doctor_title: str = ""
    department_name: str = ""
    work_date: date | None = None
    period: str = ""
    start_time: time | None = None
    end_time: time | None = None


class TriageRequest(BaseModel):
    """症状描述。"""

    symptom: str = Field(min_length=1, max_length=500)
    is_child: bool = False
    top_k: int = Field(default=3, ge=1, le=5)

    @field_validator("symptom")
    @classmethod
    def _clean_symptom(cls, v: str) -> str:
        # 先去掉首尾空白，再判断是不是真填了内容
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("请描述一下症状")
        return cleaned


class TriageCandidate(BaseModel):
    department_id: int
    department_name: str
    score: float
    reason: str = ""


class TriageResponse(BaseModel):
    symptom: str
    candidates: list[TriageCandidate]
    confidence: float
    # 置信度低于阈值时为 True，前端应提示"建议到导诊台"
    need_human: bool
    advice: str = ""


# ----------------------------------------------------------------------
# 药房与取药
# ----------------------------------------------------------------------
class MedicineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    spec: str = ""
    unit: str = "盒"
    manufacturer: str = ""
    price_yuan: float = 0.0
    stock: int = 0
    # 库存低于警戒线，前端可以提示"备货紧张"
    low: bool = False


class PrescriptionItemIn(BaseModel):
    medicine_id: int
    quantity: int = Field(default=1, gt=0, le=20)
    dosage: str = Field(default="", max_length=120)


class PrescriptionCreate(BaseModel):
    """医生开方。真实系统里这个接口要有医生身份校验，这里从简。"""

    patient_id: int
    doctor_id: int
    appointment_id: int | None = None
    items: list[PrescriptionItemIn] = Field(min_length=1)


class PrescriptionItemOut(BaseModel):
    medicine_id: int
    name: str
    spec: str = ""
    unit: str = "盒"
    quantity: int
    dosage: str = ""
    unit_price_yuan: float = 0.0
    subtotal_yuan: float = 0.0
    in_stock: bool = True


class PrescriptionOut(BaseModel):
    id: int
    code: str
    status: str
    status_text: str
    total_yuan: float
    pickup_date: date | None = None
    pickup_period: str = ""
    created_at: datetime | None = None
    items: list[PrescriptionItemOut] = []


class PickupOption(BaseModel):
    date: str
    label: str
    periods: list[str]


class PickupChoose(BaseModel):
    pickup_date: date
    period: str = Field(min_length=1, max_length=20)
