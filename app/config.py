import os

# SQLAlchemy 连接串，Compose 中注入指向 db 服务；本地开发默认连本机。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://duty:duty@localhost:5432/duty",
)

# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "60"))
DB_CONNECT_INTERVAL = float(os.getenv("DB_CONNECT_INTERVAL", "1"))
