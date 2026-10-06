import os

# SQLAlchemy 连接串，Compose 中注入指向 db 服务；本地开发默认连本机。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://duty:duty@localhost:5432/duty",
)

# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "60"))
DB_CONNECT_INTERVAL = float(os.getenv("DB_CONNECT_INTERVAL", "1"))

# ------------------------------------------------------------ 违规通知异步投递
# 保持原有启动方式：以下均有默认值，不配也能跑（投递器为进程内后台守护线程）。

# 轮询待投递队列的间隔（秒）
DISPATCH_POLL_INTERVAL = float(os.getenv("DISPATCH_POLL_INTERVAL", "0.5"))
# 单次回调请求超时（秒）：超时按失败处理并保留待重试
DISPATCH_TIMEOUT = float(os.getenv("DISPATCH_TIMEOUT", "5"))
# 失败重试退避：next_delay = min(RETRY_BASE * RETRY_FACTOR ** (attempts-1), RETRY_MAX)
DISPATCH_RETRY_BASE = float(os.getenv("DISPATCH_RETRY_BASE", "1"))
DISPATCH_RETRY_FACTOR = float(os.getenv("DISPATCH_RETRY_FACTOR", "2"))
DISPATCH_RETRY_MAX = float(os.getenv("DISPATCH_RETRY_MAX", "300"))
# 每轮最多认领多少个“(订阅, 司机) 队头”
DISPATCH_BATCH_SIZE = int(os.getenv("DISPATCH_BATCH_SIZE", "32"))
