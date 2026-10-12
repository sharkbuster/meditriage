"""对运行中的服务做一次端到端验证（HTTP 层，不走 TestClient）。

用法：先启动服务，然后 python -m scripts.smoke_live
      （也可以 python scripts/smoke_live.py，下面已处理路径）
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# 直接跑脚本时（python scripts/smoke_live.py），sys.path[0] 是 scripts/，
# 收尾清理要 import app.* 就会失败。这里显式把项目根目录加进来。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
# 演示手机号：seed.py 造的，脚本结束时会清掉自己新建的数据
DEMO_PHONE_A = "13800000001"
DEMO_PHONE_B = "13800000002"
ok_count = 0
fail_count = 0
created_rx_ids: list[int] = []


def call(method: str, path: str, body: dict | None = None):
    global ok_count, fail_count
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # 查询串里可能有中文（比如按药名搜索），必须先做百分号编码，
    # 否则 http.client 会用 ascii 编码 URL 并抛 UnicodeEncodeError。
    if "?" in path:
        base_path, _, query = path.partition("?")
        path = base_path + "?" + urllib.parse.quote(query, safe="=&")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            ok_count += 1
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        ok_count += 1
        return exc.code, (json.loads(raw) if raw else None)
    except Exception as exc:                        # noqa: BLE001
        fail_count += 1
        print(f"  ✗ {method} {path} 连接失败：{exc}")
        return 0, None


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))


print("=" * 66)
print("一、基础接口")
print("=" * 66)
status, health = call("GET", "/api/health")
check("健康检查", status == 200 and health.get("status") == "ok")

status, depts = call("GET", "/api/departments")
check("科室列表", status == 200 and len(depts) == 14, f"{len(depts)} 个科室")

status, docs = call("GET", "/api/doctors")
names = [d["name"] for d in docs]
check("医生列表", status == 200 and len(docs) == 28, f"{len(docs)} 名")
check("无假名字（不含『门诊医师』）", not any("门诊医师" in n for n in names))

print()
print("=" * 66)
print("二、药房接口")
print("=" * 66)
status, meds = call("GET", "/api/medicines")
check("药品目录", status == 200 and len(meds) == 24, f"{len(meds)} 种")
if meds:
    sample = meds[0]
    print(f"     例：{sample['name']} {sample['spec']} "
          f"{sample['price_yuan']} 元 库存 {sample['stock']}")

status, options = call("GET", "/api/pickup-options")
check("取药时段选项", status == 200 and len(options) >= 2,
      f"{len(options)} 天可选" if status == 200 else "")

status, low = call("GET", "/api/medicines/low-stock")
check("补货提醒接口", status == 200, f"{len(low)} 种需补货" if status == 200 else "")

status, search = call("GET", "/api/medicines?keyword=布洛芬")
check("药品搜索", status == 200 and len(search) >= 1,
      f"搜到 {len(search)} 种" if status == 200 else "")

print()
print("=" * 66)
print("三、患者处方查询（演示手机号）")
print("=" * 66)
status, rxs = call("GET", "/api/prescriptions?phone=13800000001")
check("患者甲处方", status == 200 and len(rxs) >= 1, f"{len(rxs)} 张")
for rx in rxs or []:
    print(f"     处方号 {rx['code']}  状态 {rx['status_text']}  "
          f"合计 {rx['total_yuan']} 元  {len(rx['items'])} 味药")
    for it in rx["items"]:
        print(f"        - {it['name']} {it['spec']} ×{it['quantity']}{it['unit']}  {it['dosage']}")

status, rxs2 = call("GET", "/api/prescriptions?phone=13800000002")
check("患者乙处方", status == 200 and len(rxs2) >= 1, f"{len(rxs2)} 张")

status, none_rx = call("GET", "/api/prescriptions?phone=13700000000")
check("陌生手机号返回空列表", status == 200 and none_rx == [])

print()
print("=" * 66)
print("四、完整取药流程（新开一张处方走一遍）")
print("=" * 66)
status, med_list = call("GET", "/api/medicines?only_in_stock=true")
if status == 200 and len(med_list) >= 2:
    m1, m2 = med_list[0], med_list[1]
    stock1_before, stock2_before = m1["stock"], m2["stock"]

    # 找一位演示患者：先按手机号约个号，再开方
    # 直接开方需要 patient_id，演示数据里患者甲是 1 号
    status, rx = call("POST", "/api/prescriptions", {
        "patient_id": 1,
        "doctor_id": 1,
        "items": [
            {"medicine_id": m1["id"], "quantity": 2, "dosage": "口服，一次1粒，一日2次"},
            {"medicine_id": m2["id"], "quantity": 1, "dosage": "口服，一次1片，一日3次"},
        ],
    })
    check("开处方", status == 201, f"处方号 {rx['code']}" if rx else "")
    if status == 201:
        rx_id = rx["id"]
        expected = round(m1["price_yuan"] * 2 + m2["price_yuan"] * 1, 2)
        check("金额计算正确", abs(rx["total_yuan"] - expected) < 0.001,
              f"{rx['total_yuan']} 元（期望 {expected}）")
        check("开方后状态为待缴费", rx["status"] == "pending_pay", rx["status_text"])

        # 未缴费不能选取药时间
        status_bad, err = call("POST", f"/api/prescriptions/{rx_id}/pickup", {
            "pickup_date": date.today().isoformat(),
            "period": "上午 08:00–12:00",
        })
        check("未缴费时选取药时间被拒", status_bad == 409,
              err.get("detail", "") if err else "")

        # 缴费
        status_pay, paid = call("POST", f"/api/prescriptions/{rx_id}/pay")
        check("缴费成功", status_pay == 200 and paid["status"] == "pending_pickup",
              paid["status_text"] if paid else "")

        status, after = call("GET", "/api/medicines?only_in_stock=true")
        now1 = next((m["stock"] for m in after if m["id"] == m1["id"]), None)
        now2 = next((m["stock"] for m in after if m["id"] == m2["id"]), None)
        check("缴费后扣库存", now1 == stock1_before - 2 and now2 == stock2_before - 1,
              f"{m1['name']} {stock1_before}->{now1}，{m2['name']} {stock2_before}->{now2}")

        # 选取药时间
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        status_pick, picked = call("POST", f"/api/prescriptions/{rx_id}/pickup", {
            "pickup_date": tomorrow,
            "period": "下午 14:00–17:30",
        })
        check("选取药时间", status_pick == 200 and picked["pickup_period"] == "下午 14:00–17:30",
              f"{picked['pickup_date']} {picked['pickup_period']}" if picked else "")

        # 药房发药
        status_done, done = call("POST", f"/api/prescriptions/{rx_id}/picked")
        check("标记已取药", status_done == 200 and done["status"] == "picked",
              done["status_text"] if done else "")

        # 已取药不能作废
        status_cancel, cerr = call("POST", f"/api/prescriptions/{rx_id}/cancel")
        check("已取药不能作废", status_cancel == 409,
              cerr.get("detail", "") if cerr else "")

        created_rx_ids.append(rx_id)

print()
print("=" * 66)
print("五、页面")
print("=" * 66)
try:
    with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    check("患者首页可访问", resp.status == 200)
    check("含取药标签", 'data-view="pickup"' in html)
    check("含取药视图", 'id="view-pickup"' in html)
    check("不含技术词", not any(w in html for w in ("patient_id", "confidence", "schedule_id")))
except Exception as exc:                            # noqa: BLE001
    check("患者首页可访问", False, str(exc))

print()
print("=" * 66)
print(f"通过 {ok_count} 项，失败 {fail_count} 项")
print("=" * 66)

# 清掉本次跑测产生的处方，免得污染演示数据（seed 造的演示处方不动）
if created_rx_ids:
    from app.db.session import SessionLocal
    from app.services import pharmacy

    db = SessionLocal()
    try:
        # 删除走 service 层统一入口，顺序（先明细后主表）由它负责
        removed = pharmacy.delete_prescriptions(db, created_rx_ids)
        db.commit()
        print(f"已清理本次验证产生的 {removed} 张处方")
    except Exception as exc:                        # noqa: BLE001
        db.rollback()
        print(f"清理失败（不影响验证结论）：{type(exc).__name__}: {exc}")
    finally:
        db.close()

sys.exit(1 if fail_count else 0)
