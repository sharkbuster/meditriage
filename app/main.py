"""FastAPI 应用入口。

患者界面：  http://127.0.0.1:8000/          （纯中文，给患者用）
接口文档：  http://127.0.0.1:8000/docs      （开发者用，自动生成）

本地启动：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.db.session import init_db

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表。真正上线应该交给 Alembic 迁移，这里图省事。
    init_db()
    yield


settings = get_settings()

app = FastAPI(
    title=settings.app_name + "（接口文档）",
    description=(
        "这是给开发者看的接口文档。患者请直接访问首页 `/`。\n\n"
        "号源扣减保证不超卖，分诊默认零依赖可离线运行。"
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# 患者端页面直接返静态文件，不上 Jinja2：省一个依赖，
# 数据全由前端调 API 拿，保持 clone 下来零配置能跑。
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """患者界面首页。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
