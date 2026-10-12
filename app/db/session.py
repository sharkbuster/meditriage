"""数据库连接与会话管理。"""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# SQLite 需要关闭同线程检查，否则并发测试里多线程共用一个连接会报错
is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)


def enable_sqlite_pragmas(dbapi_conn) -> None:
    """设好 SQLite 要的 PRAGMA。

    单独抽出来是给测试用的：测试库不开 foreign_keys 的话，
    脏数据在测试里写得进去、上线才报错。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """连上就打开外键。

    SQLite 默认 foreign_keys=0，外键形同虚设：指向不存在患者的处方
    照样能写进去。换成 PostgreSQL 同样的代码直接报错，
    本地测不出来、上线才炸，所以这里显式打开。
    """
    if not is_sqlite:
        return
    enable_sqlite_pragmas(dbapi_conn)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.domain import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
