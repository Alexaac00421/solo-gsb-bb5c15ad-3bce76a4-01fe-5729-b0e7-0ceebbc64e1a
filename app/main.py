import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from . import models  # noqa: F401  确保元数据已注册
from .api import router
from .config import DB_CONNECT_INTERVAL, DB_CONNECT_RETRIES
from .db import Base, engine


def _init_db_with_retry() -> None:
    """等待数据库就绪并自动建表（初始化数据库）。"""
    last_err: Exception | None = None
    for _ in range(DB_CONNECT_RETRIES):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:  # 数据库尚未就绪
            last_err = exc
            time.sleep(DB_CONNECT_INTERVAL)
    raise RuntimeError(f"数据库不可用，初始化失败: {last_err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="铁路司机值勤合规核算服务",
    version="1.2.0",
    description=(
        "司机值勤片段录入/更正、滚动 24h 值勤上限与累计驾驶上限核算、"
        "跨司机值勤批量调整（批次原子提交）、审核发布与历史对照、违规告警确认"
    ),
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/", tags=["meta"])
def root():
    return {"service": "rail-driver-duty-compliance", "docs": "/docs", "health": "/health"}


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
