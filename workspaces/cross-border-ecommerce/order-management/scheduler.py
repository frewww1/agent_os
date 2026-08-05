"""定时任务调度 — 定期同步订单、更新物流、检查异常"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

from sync import get_syncer
from logistics import tracker
from alerts import monitor
from profit import calculator
from models import Platform


scheduler = BackgroundScheduler()


def sync_all_platforms():
    """同步所有平台最近1天订单"""
    for platform in [Platform.SHOPEE, Platform.LAZADA]:
        try:
            syncer = get_syncer(platform)
            start = datetime.utcnow() - timedelta(days=1)
            count = syncer.sync(start)
            print(f"[{datetime.now()}] {platform.value} sync: {count} orders")
        except Exception as e:
            print(f"[{datetime.now()}] {platform.value} sync error: {e}")


def update_logistics():
    """更新在途物流"""
    try:
        count = tracker.batch_update_all_active()
        print(f"[{datetime.now()}] logistics updated: {count} orders")
    except Exception as e:
        print(f"[{datetime.now()}] logistics update error: {e}")


def check_alerts():
    """执行异常检查"""
    try:
        alerts = monitor.check_all()
        if alerts:
            print(f"[{datetime.now()}] alerts created: {len(alerts)}")
            for a in alerts:
                print(f"  - [{a.severity}] {a.message}")
    except Exception as e:
        print(f"[{datetime.now()}] alert check error: {e}")


def recalculate_profits():
    """重算最近7天订单利润"""
    try:
        count = calculator.batch_update_profits()
        print(f"[{datetime.now()}] profit recalculated: {count} orders")
    except Exception as e:
        print(f"[{datetime.now()}] profit recalc error: {e}")


def start_scheduler():
    """启动定时任务"""
    # 每30分钟同步一次订单
    scheduler.add_job(sync_all_platforms, "interval", minutes=30, id="sync_orders")
    # 每1小时更新物流
    scheduler.add_job(update_logistics, "interval", minutes=60, id="update_logistics")
    # 每30分钟检查异常
    scheduler.add_job(check_alerts, "interval", minutes=30, id="check_alerts")
    # 每天凌晨2点重算利润
    scheduler.add_job(recalculate_profits, "cron", hour=2, minute=0, id="recalc_profits")

    scheduler.start()
    print("[Scheduler] All jobs started.")


def stop_scheduler():
    scheduler.shutdown()
