"""项目自检：目录结构 + 测试 + 数据 + 应用启动。

    .venv\\Scripts\\python.exe scripts/selfcheck.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REQUIRED = [
    "README.md", "LICENSE", ".gitignore", ".env.example", "requirements.txt", "pytest.ini",
    "Dockerfile", "docker-compose.yml", ".github/workflows/ci.yml",
    "app/main.py", "app/core/config.py", "app/db/session.py",
    "app/domain/models.py", "app/domain/schemas.py",
    "app/knowledge/departments.py",
    "app/services/appointment.py", "app/services/booking.py",
    "app/services/triage.py", "app/services/llm.py", "app/services/pharmacy.py",
    "app/api/routes.py",
    "app/static/index.html", "app/static/style.css", "app/static/app.js",
    "scripts/seed.py", "scripts/evaluate.py", "scripts/loadtest.py",
    "scripts/show_data.py", "scripts/cleanup_test_data.py", "scripts/smoke_live.py",
    "data/triage_eval.csv",
    "tests/test_concurrency.py", "tests/test_api.py",
    "tests/test_frontend.py", "tests/test_pharmacy.py",
]


def check_files() -> int:
    print("=" * 62)
    print("1. 文件完整性")
    print("=" * 62)
    missing = [f for f in REQUIRED if not (ROOT / f).exists()]
    for f in REQUIRED:
        print(f"   {'OK  ' if (ROOT / f).exists() else '缺失'} {f}")
    return len(missing)


def check_import() -> int:
    print()
    print("=" * 62)
    print("2. 导入与建表")
    print("=" * 62)
    try:
        from app.db.session import Base, engine, init_db
        import app.domain.models  # noqa: F401
        from app.main import app

        init_db()
        tables = sorted(Base.metadata.tables)

        # 走 OpenAPI 而不是遍历 app.routes：FastAPI 0.143 起
        # include_router 会包一层 _IncludedRouter，扒内部结构太依赖版本。
        paths = app.openapi()["paths"]
        endpoints = [(method.upper(), path)
                     for path, ops in paths.items()
                     for method in ops]
        print(f"   FastAPI 应用：{app.title}")
        print(f"   业务接口：{len(endpoints)} 个")
        for method, path in sorted(endpoints, key=lambda x: (x[1], x[0])):
            print(f"      {method:<6} {path}")
        print(f"   数据表({len(tables)})：{', '.join(tables)}")
        return 0
    except Exception as exc:                       # noqa: BLE001
        print(f"   失败：{type(exc).__name__}: {exc}")
        return 1


def check_triage() -> int:
    print()
    print("=" * 62)
    print("3. 分诊引擎（rule 模式，零依赖）")
    print("=" * 62)
    try:
        from app.services.triage import TriageService

        svc = TriageService()
        for symptom in ("咳嗽三天了还发烧，喘不上气", "肚子疼拉肚子", "不太舒服"):
            r = svc.triage(symptom)
            top = r["candidates"][0]["department_name"] if r["candidates"] else "(无)"
            print(f"   {symptom:<18} -> {top:<8} 覆盖率={r['confidence']:<7} "
                  f"转人工={r['need_human']}")
        return 0
    except Exception as exc:                       # noqa: BLE001
        print(f"   失败：{type(exc).__name__}: {exc}")
        return 1


def check_pharmacy() -> int:
    print()
    print("=" * 62)
    print("4. 药房与取药")
    print("=" * 62)
    try:
        from sqlalchemy import func, select

        from app.db.session import SessionLocal
        from app.domain.models import Medicine, Prescription
        from app.services import pharmacy

        with SessionLocal() as db:
            med_count = db.scalar(select(func.count()).select_from(Medicine)) or 0
            rx_count = db.scalar(select(func.count()).select_from(Prescription)) or 0
            low = pharmacy.low_stock(db)
            print(f"   药品目录：{med_count} 种")
            print(f"   处方：{rx_count} 张")
            print(f"   需补货：{len(low)} 种")
            print(f"   取药时段：{len(pharmacy.pickup_options())} 天可选")
            print(f"   状态文案：{pharmacy.STATUS_TEXT}")
        if med_count == 0:
            print("   ⚠️ 药品目录是空的，先跑 python -m scripts.seed")
        return 0
    except Exception as exc:                       # noqa: BLE001
        print(f"   失败：{type(exc).__name__}: {exc}")
        return 1


def check_tests() -> int:
    print()
    print("=" * 62)
    print("5. 测试套件")
    print("=" * 62)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-3:]
    for ln in tail:
        print(f"   {ln}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    problems = (check_files() + check_import() + check_triage()
                + check_pharmacy() + check_tests())
    print()
    print("=" * 62)
    print("自检通过 ✅" if problems == 0 else f"自检发现 {problems} 个问题 ❌")
    print("=" * 62)
    sys.exit(1 if problems else 0)
