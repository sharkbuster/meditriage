"""生成演示数据：科室、医生、排班、患者。

跑一次就能把系统填满，方便直接打开 /docs 试接口。

    .venv\\Scripts\\python.exe -m scripts.seed

重复执行不会产生重复数据（按唯一字段先查后插）。
"""
from __future__ import annotations

import sys
from datetime import date, time, timedelta
from pathlib import Path

# 允许用 python scripts/seed.py 直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select                     # noqa: E402

from app.db.session import SessionLocal, init_db   # noqa: E402
from app.domain.models import (                    # noqa: E402
    Department,
    Doctor,
    Medicine,
    Patient,
    Prescription,
    Schedule,
)
from app.knowledge.departments import DEPARTMENTS  # noqa: E402
from app.services.triage import DepartmentIndex    # noqa: E402

# 每个科室 2 名医生，名字写死。
# 别用「姓氏 + 门诊医师」去拼：之前拼出"张门诊医师""李门诊医师"，
# 14 个科各来一遍，看着就像数据重复。
DOCTORS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "呼吸内科": (
        ("张建国", "主任医师", "慢性咳嗽、慢阻肺、肺部感染"),
        ("周雅琴", "副主任医师", "哮喘、肺结节随访"),
    ),
    "消化内科": (
        ("李秀兰", "副主任医师", "胃食管反流、幽门螺杆菌根除"),
        ("许志远", "主治医师", "肠易激综合征、慢性肝病"),
    ),
    "心血管内科": (
        ("王海涛", "主任医师", "冠心病介入、高血压个体化用药"),
        ("秦月华", "副主任医师", "心律失常、心力衰竭"),
    ),
    "神经内科": (
        ("陈美玲", "主治医师", "偏头痛、眩晕、睡眠障碍"),
        ("蒋文斌", "副主任医师", "脑卒中二级预防、帕金森病"),
    ),
    "骨科": (
        ("刘志强", "副主任医师", "膝关节置换、运动损伤"),
        ("方振宇", "主治医师", "腰椎间盘突出、骨折复位"),
    ),
    "皮肤科": (
        ("赵雪", "主治医师", "湿疹、荨麻疹、痤疮"),
        ("孙丽娟", "副主任医师", "银屑病、脱发、色素性疾病"),
    ),
    "耳鼻咽喉科": (
        ("孙鹏", "主治医师", "过敏性鼻炎、慢性咽炎"),
        ("邓春燕", "副主任医师", "中耳炎、打鼾与睡眠呼吸暂停"),
    ),
    "眼科": (
        ("周敏", "副主任医师", "干眼症、白内障术前评估"),
        ("曹立新", "主治医师", "青光眼、眼底病变"),
    ),
    "泌尿外科": (
        ("吴国平", "主任医师", "泌尿系结石、前列腺增生"),
        ("韩雪松", "主治医师", "尿路感染、血尿排查"),
    ),
    "妇科": (
        ("郑丽华", "副主任医师", "月经失调、多囊卵巢综合征"),
        ("冯晓雯", "主治医师", "阴道炎、孕期保健"),
    ),
    "儿科": (
        ("马晓东", "主治医师", "小儿呼吸道感染、腹泻"),
        ("唐婉如", "副主任医师", "儿童生长发育、新生儿黄疸"),
    ),
    "内分泌科": (
        ("何静", "副主任医师", "糖尿病、甲状腺结节"),
        ("谢明辉", "主治医师", "痛风、肥胖与代谢综合征"),
    ),
    "精神心理科": (
        ("林浩", "主治医师", "焦虑障碍、抑郁状态"),
        ("苏婉清", "副主任医师", "睡眠障碍、强迫症"),
    ),
    "口腔科": (
        ("黄燕", "主治医师", "牙体牙髓病、牙周治疗"),
        ("邱正阳", "副主任医师", "阻生智齿拔除、种植修复"),
    ),
}

# 药品目录，价格单位是分（展示时除以 100）。
# 挑了门诊最常见的几类：解热镇痛、抗感染、感冒止咳、胃药、心血管、降糖、抗过敏、外用。
MEDICINES: tuple[tuple[str, str, str, str, int, int], ...] = (
    # (名称, 规格, 单位, 生产厂家, 单价分, 初始库存)
    ("布洛芬缓释胶囊", "0.3g*20粒", "盒", "中美天津史克", 1850, 120),
    ("对乙酰氨基酚片", "0.5g*20片", "盒", "广州白云山", 680, 200),
    ("阿莫西林胶囊", "0.25g*24粒", "盒", "华北制药", 1580, 90),
    ("头孢克肟分散片", "0.1g*12片", "盒", "齐鲁制药", 3200, 60),
    ("阿奇霉素片", "0.25g*6片", "盒", "辉瑞制药", 4560, 40),
    ("复方甘草片", "100片", "瓶", "太极集团", 890, 70),
    ("氨溴索口服液", "100ml", "瓶", "勃林格殷格翰", 2680, 55),
    ("感冒灵颗粒", "10g*9袋", "盒", "华润三九", 1280, 150),
    ("连花清瘟胶囊", "0.35g*24粒", "盒", "以岭药业", 2350, 80),
    ("氯雷他定片", "10mg*6片", "盒", "上海先灵葆雅", 1560, 100),
    ("奥美拉唑肠溶胶囊", "20mg*14粒", "盒", "阿斯利康", 3480, 65),
    ("铝碳酸镁咀嚼片", "0.5g*20片", "盒", "拜耳医药", 1890, 85),
    ("蒙脱石散", "3g*10袋", "盒", "博福益普生", 1420, 110),
    ("双歧杆菌三联活菌", "210mg*36粒", "盒", "上海信谊", 3680, 45),
    ("硝苯地平控释片", "30mg*7片", "盒", "拜耳医药", 2880, 75),
    ("厄贝沙坦片", "150mg*7片", "盒", "赛诺菲", 3240, 70),
    ("二甲双胍缓释片", "0.5g*30片", "盒", "中美上海施贵宝", 1980, 95),
    ("阿托伐他汀钙片", "20mg*7片", "盒", "辉瑞制药", 3860, 50),
    ("维生素C片", "100mg*100片", "瓶", "华中药业", 560, 180),
    ("炉甘石洗剂", "100ml", "瓶", "上海运佳黄浦", 780, 130),
    ("糠酸莫米松乳膏", "5g", "支", "拜耳医药", 2980, 48),
    ("红霉素眼膏", "2g", "支", "白云山制药", 620, 90),
    ("氧氟沙星滴耳液", "5ml", "支", "沈阳兴齐", 1150, 42),
    ("生理性海水鼻腔喷雾", "60ml", "瓶", "江苏小满", 3480, 30),
)


def seed_medicines(db) -> int:
    """灌药品目录。按名称去重，重复执行安全。"""
    created = 0
    for name, spec, unit, maker, price, stock in MEDICINES:
        exists = db.scalar(select(Medicine).where(Medicine.name == name))
        if exists is None:
            db.add(Medicine(name=name, spec=spec, unit=unit, manufacturer=maker,
                            price_cents=price, stock=stock,
                            warn_stock=max(5, stock // 10)))
            created += 1
    db.commit()
    return created


def reset_pharmacy(db) -> None:
    """把药房数据恢复到"刚 seed 完"的状态。

    为什么需要这个：演示处方只有两张（一张待缴费、一张待取药），
    跑测试或手动点几下就会把它们改成"已取药"，演示流程就没法演了。
    而 seed_demo_prescriptions 的幂等判断是"该患者已有处方就跳过"，
    它**只会补建、不会修复**，所以恢复不了。

    做法：清空所有处方 → 把药品库存重置回目录里的初始值 → 重建演示处方。
    只动药房相关的表，预约和排班不受影响。
    """
    from app.services import pharmacy

    # 删除走 service 层的统一入口，避免各处自己写 delete 时漏删明细
    pharmacy.delete_prescriptions(db)
    db.commit()

    for name, _spec, _unit, _maker, _price, stock in MEDICINES:
        med = db.scalar(select(Medicine).where(Medicine.name == name))
        if med is not None:
            med.stock = stock
    db.commit()


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        # ---- 科室 ----
        index = DepartmentIndex(DEPARTMENTS)
        name_to_id: dict[str, int] = {}
        for item in DEPARTMENTS:
            dep = db.scalar(select(Department).where(Department.name == item["name"]))
            if dep is None:
                dep = Department(
                    name=item["name"],
                    keywords=item["keywords"],
                    description=item["description"],
                    for_child=item["for_child"],
                )
                db.add(dep)
                db.flush()
            name_to_id[item["name"]] = dep.id
        db.commit()
        print(f"科室就绪：{len(name_to_id)} 个")

        # ---- 医生 ----
        doctor_ids: list[int] = []
        for dep_name, roster in DOCTORS.items():
            dep_id = name_to_id[dep_name]
            for doc_name, doc_title, doc_spec in roster:
                exists = db.scalar(
                    select(Doctor).where(Doctor.name == doc_name, Doctor.department_id == dep_id)
                )
                if exists is None:
                    exists = Doctor(name=doc_name, title=doc_title,
                                    department_id=dep_id, specialty=doc_spec)
                    db.add(exists)
                    db.flush()
                doctor_ids.append(exists.id)
        db.commit()
        print(f"医生就绪：{len(doctor_ids)} 名")

        # ---- 患者 ----
        patients = [
            ("测试患者甲", "13800000001", "460100200401011234", False),
            ("测试患者乙", "13800000002", "460100200401015678", False),
            ("测试患儿丙", "13800000003", "460100202001019012", True),
        ]
        for name, phone, id_card, is_child in patients:
            if db.scalar(select(Patient).where(Patient.phone == phone)) is None:
                db.add(Patient(name=name, phone=phone, id_card=id_card, is_child=is_child))
        db.commit()
        print(f"患者就绪：{len(patients)} 名")

        # ---- 排班：未来 7 天，上午/下午各一波 ----
        today = date.today()
        created = 0
        for offset in range(1, 8):
            work_date = today + timedelta(days=offset)
            if work_date.weekday() >= 5:          # 周末不排普通门诊
                continue
            for doctor_id in doctor_ids:
                for period, start, end in (
                    ("上午", time(8, 30), time(11, 30)),
                    ("下午", time(14, 0), time(17, 0)),
                ):
                    exists = db.scalar(
                        select(Schedule).where(
                            Schedule.doctor_id == doctor_id,
                            Schedule.work_date == work_date,
                            Schedule.period == period,
                        )
                    )
                    if exists is not None:
                        continue
                    # 号源数做成小数字（3~8），方便演示和压测"抢光"
                    capacity = 3 + (doctor_id + offset) % 6
                    db.add(Schedule(
                        doctor_id=doctor_id, work_date=work_date, period=period,
                        start_time=start, end_time=end,
                        capacity=capacity, remaining=capacity,
                    ))
                    created += 1
        db.commit()
        print(f"排班就绪：新增 {created} 条")

        # ---- 药品目录 ----
        med_created = seed_medicines(db)
        total_med = db.scalar(select(func.count()).select_from(Medicine))
        print(f"药品就绪：新增 {med_created} 种，目录共 {total_med} 种")

        # ---- 演示处方 ----
        seeded_rx = seed_demo_prescriptions(db)
        if seeded_rx:
            print(f"演示处方：新增 {seeded_rx} 张")

        print()
        print("提示：数据已写入，直接打开 http://127.0.0.1:8000/ 就能用")
        print("      想看接口文档：http://127.0.0.1:8000/docs")
        print("      演示手机号：13800000001（患者甲）、13800000002（患者乙）")
        print("      把药房数据恢复成初始状态：python -m scripts.seed --reset-pharmacy")
    finally:
        db.close()


def seed_reset_pharmacy() -> None:
    """把药房相关数据重置回初始状态，然后重建演示处方。"""
    init_db()
    db = SessionLocal()
    try:
        reset_pharmacy(db)
        print("已清空处方并重置药品库存")
        created = seed_demo_prescriptions(db)
        print(f"演示处方已重建：{created} 张")
        for rx in db.scalars(select(Prescription).order_by(Prescription.id)):
            patient = db.get(Patient, rx.patient_id)
            names = "、".join(i.medicine.name for i in rx.items)
            print(f"   {patient.name}  {rx.code}  {rx.status}  "
                  f"{rx.total_cents / 100} 元  [{names}]")
    finally:
        db.close()


# 演示处方：让取药功能一打开就有东西可看，不用先去开方。
# 真实系统里处方只能由医生开，这里是为了演示数据完整性。
DEMO_PRESCRIPTIONS: tuple[tuple[str, tuple[tuple[str, int, str], ...]], ...] = (
    # (患者手机号, ((药品名, 数量, 用法用量), ...))
    ("13800000001", (
        ("布洛芬缓释胶囊", 1, "口服，一次1粒，一日2次，饭后服用"),
        ("氨溴索口服液", 1, "口服，一次10ml，一日3次"),
    )),
    ("13800000002", (
        ("奥美拉唑肠溶胶囊", 1, "口服，一次1粒，一日1次，早餐前服用"),
        ("铝碳酸镁咀嚼片", 2, "口服，一次2片，一日3次，嚼碎后服用"),
        ("蒙脱石散", 1, "口服，一次1袋，一日3次，温水冲服"),
    )),
)


def seed_demo_prescriptions(db) -> int:
    """造几张演示处方：一张待缴费，一张已缴费待取药。"""
    from app.domain.models import Prescription, PrescriptionItem
    from app.services import pharmacy

    created = 0
    statuses = ("pending_pay", "pending_pickup")   # 两种状态各来一张，界面都能看到

    for idx, (phone, lines) in enumerate(DEMO_PRESCRIPTIONS):
        patient = db.scalar(select(Patient).where(Patient.phone == phone))
        if patient is None:
            continue
        # 已经建过就不重复建
        existed = db.scalar(
            select(Prescription).where(Prescription.patient_id == patient.id).limit(1))
        if existed is not None:
            continue

        # 随便找个医生当开方医生（演示用）
        doctor = db.scalar(select(Doctor).order_by(Doctor.id))
        if doctor is None:
            continue

        target_status = statuses[idx % len(statuses)]
        rx = pharmacy.create_prescription(
            db,
            patient_id=patient.id,
            doctor_id=doctor.id,
            items=[
                {"medicine_id": med.id, "quantity": qty, "dosage": dosage}
                for name, qty, dosage in lines
                for med in [db.scalar(select(Medicine).where(Medicine.name == name))]
                if med is not None
            ],
        )
        if target_status == "pending_pickup":
            # 缴费会真的扣库存，这样演示数据里"待取药"那张的库存状态也是真的
            pharmacy.pay(db, rx.id)
        created += 1

    return created


if __name__ == "__main__":
    if "--reset-pharmacy" in sys.argv:
        seed_reset_pharmacy()
    else:
        seed()
