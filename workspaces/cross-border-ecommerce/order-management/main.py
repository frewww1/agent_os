"""订单管理模块入口 — 启动 FastAPI 服务"""
import uvicorn
from api import app
from scheduler import start_scheduler, stop_scheduler
from models import init_db


def main():
    print("=" * 50)
    print("  订单管理模块 - Order Management System")
    print("=" * 50)

    # 初始化数据库
    init_db()
    print("[OK] Database initialized.")

    # 启动定时任务
    start_scheduler()

    # 启动 API 服务
    print("[OK] Starting API server on http://0.0.0.0:8002")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()
