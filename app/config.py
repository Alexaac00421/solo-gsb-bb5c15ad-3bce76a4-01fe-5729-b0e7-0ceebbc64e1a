import os

# SQLAlchemy 连接串，Compose 中注入指向 db 服务；本地开发默认连本机。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://duty:duty@localhost:5432/duty",
)

# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "60"))
DB_CONNECT_INTERVAL = float(os.getenv("DB_CONNECT_INTERVAL", "1"))

# 违规事件回调投递器
DISPATCH_ENABLED = os.getenv("DISPATCH_ENABLED", "1") != "0"  # 置 0 可关闭后台投递
DISPATCH_POLL_INTERVAL = float(os.getenv("DISPATCH_POLL_INTERVAL", "1"))  # 轮询间隔（秒）
DISPATCH_TIMEOUT = float(os.getenv("DISPATCH_TIMEOUT", "5"))  # 单次回调 HTTP 超时（秒）
DISPATCH_WORKERS = int(os.getenv("DISPATCH_WORKERS", "4"))  # 并行投递的（订阅,司机）组数
DISPATCH_RETRY_BASE_SECONDS = float(os.getenv("DISPATCH_RETRY_BASE_SECONDS", "2"))  # 退避基数
DISPATCH_RETRY_MAX_SECONDS = float(os.getenv("DISPATCH_RETRY_MAX_SECONDS", "60"))  # 退避上限
