"""订单管理 API — FastAPI 路由"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import Session

from config import config
from models import (
    Order, OrderItem, OrderStatus, Platform, LogisticsRecord, Alert,
    ProfitReport, init_db, engine,
)
from sync import get_syncer
from logistics import tracker
from profit import calculator
from alerts import monitor

app = FastAPI(title="订单管理 API", version="1.0.0")


# ======================== Schema ========================

class OrderCreate(BaseModel):
    platform_order_id: str
    platform: Platform
    shop_name: str = ""
    buyer_name: str = ""
    buyer_phone: str = ""
    buyer_address: str = ""
    currency: str = "TWD"
    total_amount: float = 0.0
    shipping_fee: float = 0.0
    discount: float = 0.0
    actual_amount: float = 0.0
    cost_of_goods: float = 0.0
    platform_fee: float = 0.0
    logistics_cost: float = 0.0
    other_cost: float = 0.0
    note: str = ""


class OrderUpdate(BaseModel):
    status: Optional[OrderStatus] = None
    cost_of_goods: Optional[float] = None
    platform_fee: Optional[float] = None
    logistics_cost: Optional[float] = None
    other_cost: Optional[float] = None
    note: Optional[str] = None


class TrackingAdd(BaseModel):
    tracking_number: str
    carrier: str = "auto"


class AlertResolve(BaseModel):
    note: str = ""


# ======================== 启动事件 ========================

@app.on_event("startup")
def startup():
    init_db()


# ======================== 订单 CRUD ========================

@app.get("/api/orders")
def list_orders(
    platform: Optional[Platform] = None,
    status: Optional[OrderStatus] = None,
    days: int = Query(default=30, ge=1, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """订单列表"""
    with Session(engine) as session:
        query = session.query(Order)
        if platform:
            query = query.filter(Order.platform == platform)
        if status:
            query = query.filter(Order.status == status)

        start_date = datetime.utcnow() - timedelta(days=days)
        query = query.filter(Order.created_at >= start_date)

        total = query.count()
        orders = query.order_by(Order.created_at.desc()) \
            .offset((page - 1) * page_size).limit(page_size).all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "orders": [_order_to_dict(o) for o in orders],
        }


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    """订单详情"""
    with Session(engine) as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(404, "订单不存在")
        return _order_to_dict(order, include_items=True)


@app.post("/api/orders")
def create_order(data: OrderCreate):
    """手动创建订单"""
    with Session(engine) as session:
        existing = session.query(Order).filter_by(
            platform_order_id=data.platform_order_id
        ).first()
        if existing:
            raise HTTPException(409, "订单号已存在")

        order = Order(**data.model_dump())
        session.add(order)
        session.commit()
        session.refresh(order)
        return _order_to_dict(order)


@app.put("/api/orders/{order_id}")
def update_order(order_id: int, data: OrderUpdate):
    """更新订单"""
    with Session(engine) as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(404, "订单不存在")

        update_data = data.model_dump(exclude_none=True)
        for k, v in update_data.items():
            setattr(order, k, v)

        # 状态变更时更新时间戳
        now = datetime.utcnow()
        if order.status == OrderStatus.PAID and not order.paid_at:
            order.paid_at = now
        elif order.status == OrderStatus.SHIPPED and not order.shipped_at:
            order.shipped_at = now
        elif order.status == OrderStatus.DELIVERED and not order.delivered_at:
            order.delivered_at = now
        elif order.status == OrderStatus.COMPLETED and not order.completed_at:
            order.completed_at = now

        session.commit()
        session.refresh(order)
        return _order_to_dict(order)


# ======================== 同步 ========================

@app.post("/api/sync/{platform}")
def sync_orders(platform: Platform, days: int = Query(default=1, ge=1, le=30)):
    """同步平台订单"""
    syncer = get_syncer(platform)
    start_time = datetime.utcnow() - timedelta(days=days)
    count = syncer.sync(start_time)
    return {"platform": platform.value, "synced": count}


# ======================== 物流 ========================

@app.post("/api/orders/{order_id}/tracking")
def add_tracking(order_id: int, data: TrackingAdd):
    """添加物流单号"""
    record = tracker.add_tracking(order_id, data.tracking_number, data.carrier)
    return {
        "id": record.id,
        "tracking_number": record.tracking_number,
        "carrier": record.carrier,
        "status": record.status,
    }


@app.get("/api/orders/{order_id}/tracking")
def get_tracking(order_id: int):
    """查询订单物流"""
    tracker.update_order_logistics(order_id)
    with Session(engine) as session:
        records = session.query(LogisticsRecord).filter_by(order_id=order_id).all()
        return [_tracking_to_dict(r) for r in records]


@app.post("/api/tracking/batch-update")
def batch_update_tracking():
    """批量更新在途物流"""
    count = tracker.batch_update_all_active()
    return {"updated": count}


# ======================== 利润 ========================

@app.post("/api/orders/{order_id}/calculate-profit")
def calculate_order_profit(order_id: int):
    """计算单笔订单利润"""
    order = calculator.update_order_profit(order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    return {"order_id": order.id, "profit": order.profit, "profit_margin": order.profit_margin}


@app.post("/api/profit/batch-calculate")
def batch_calculate_profit():
    """批量重算利润"""
    count = calculator.batch_update_profits()
    return {"updated": count}


@app.get("/api/profit/report")
def get_profit_report(
    period: str = Query(default="daily", regex="^(daily|weekly|monthly)$"),
    platform: Optional[str] = None,
    days: int = Query(default=7, ge=1, le=90),
):
    """获取利润报表"""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    report = calculator.generate_profit_report(
        period=period,
        start_date=start_date,
        end_date=end_date,
        platform=platform,
    )
    return {
        "period": report.period,
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "platform": report.platform,
        "total_orders": report.total_orders,
        "total_revenue": report.total_revenue,
        "total_cost": report.total_cost,
        "total_profit": report.total_profit,
        "avg_profit_margin": report.avg_profit_margin,
        "return_count": report.return_count,
        "cancellation_count": report.cancellation_count,
    }


@app.get("/api/profit/summary")
def get_summary(days: int = Query(default=7, ge=1, le=90)):
    """经营摘要"""
    return calculator.get_summary(days)


# ======================== 告警 ========================

@app.post("/api/alerts/check")
def run_alert_check():
    """执行异常检查"""
    alerts = monitor.check_all()
    return {"alerts_created": len(alerts), "alerts": [_alert_to_dict(a) for a in alerts]}


@app.get("/api/alerts")
def list_alerts(
    severity: Optional[str] = None,
    resolved: Optional[bool] = False,
):
    """告警列表"""
    with Session(engine) as session:
        query = session.query(Alert)
        if severity:
            query = query.filter_by(severity=severity)
        query = query.filter_by(is_resolved=resolved)
        alerts = query.order_by(Alert.created_at.desc()).limit(50).all()
        return [_alert_to_dict(a) for a in alerts]


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, data: AlertResolve):
    """解决告警"""
    alert = monitor.resolve_alert(alert_id, data.note)
    if not alert:
        raise HTTPException(404, "告警不存在")
    return _alert_to_dict(alert)


# ======================== 辅助函数 ========================

def _order_to_dict(order: Order, include_items: bool = False) -> dict:
    result = {
        "id": order.id,
        "platform_order_id": order.platform_order_id,
        "platform": order.platform.value if hasattr(order.platform, 'value') else str(order.platform),
        "shop_name": order.shop_name,
        "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
        "buyer_name": order.buyer_name,
        "buyer_phone": order.buyer_phone,
        "currency": order.currency,
        "total_amount": order.total_amount,
        "shipping_fee": order.shipping_fee,
        "discount": order.discount,
        "actual_amount": order.actual_amount,
        "cost_of_goods": order.cost_of_goods,
        "platform_fee": order.platform_fee,
        "logistics_cost": order.logistics_cost,
        "other_cost": order.other_cost,
        "profit": order.profit,
        "profit_margin": order.profit_margin,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "note": order.note,
    }
    if include_items:
        result["items"] = [
            {
                "id": item.id,
                "platform_sku": item.platform_sku,
                "internal_sku": item.internal_sku,
                "product_name": item.product_name,
                "variant": item.variant,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "unit_cost": item.unit_cost,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ]
    return result


def _tracking_to_dict(record: LogisticsRecord) -> dict:
    return {
        "id": record.id,
        "tracking_number": record.tracking_number,
        "carrier": record.carrier,
        "status": record.status,
        "status_detail": record.status_detail,
        "estimated_delivery": record.estimated_delivery.isoformat() if record.estimated_delivery else None,
        "actual_delivery": record.actual_delivery.isoformat() if record.actual_delivery else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def _alert_to_dict(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "order_id": alert.order_id,
        "alert_type": alert.alert_type.value if hasattr(alert.alert_type, 'value') else str(alert.alert_type),
        "severity": alert.severity,
        "message": alert.message,
        "detail": alert.detail,
        "is_resolved": alert.is_resolved,
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "resolved_note": alert.resolved_note,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    }
