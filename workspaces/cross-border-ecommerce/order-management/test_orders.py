"""订单管理模块测试"""
import pytest
from datetime import datetime, timedelta

from models import (
    Order, OrderItem, OrderStatus, Platform, LogisticsRecord, Alert,
    AlertType, ProfitReport, init_db, engine,
)
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True)
def setup_db():
    """每个测试前重建表"""
    from models import Base
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def sample_order():
    """创建示例订单"""
    with Session(engine) as session:
        order = Order(
            platform_order_id="TEST-20260728-001",
            platform=Platform.SHOPEE,
            shop_name="TestShop",
            status=OrderStatus.PAID,
            buyer_name="Test Buyer",
            buyer_phone="0912345678",
            currency="TWD",
            total_amount=500.0,
            shipping_fee=60.0,
            actual_amount=450.0,
            cost_of_goods=200.0,
            platform_fee=30.0,
            logistics_cost=60.0,
            other_cost=10.0,
            paid_at=datetime.utcnow(),
        )
        session.add(order)
        session.commit()
        return order.id


class TestOrderModel:
    """订单模型测试"""

    def test_create_order(self, setup_db):
        with Session(engine) as session:
            order = Order(
                platform_order_id="SHOPEE-001",
                platform=Platform.SHOPEE,
                status=OrderStatus.UNPAID,
                currency="TWD",
                total_amount=300.0,
                actual_amount=280.0,
            )
            session.add(order)
            session.commit()
            assert order.id is not None
            assert order.platform_order_id == "SHOPEE-001"

    def test_order_unique_constraint(self, setup_db):
        with Session(engine) as session:
            order1 = Order(platform_order_id="DUP-001", platform=Platform.SHOPEE)
            session.add(order1)
            session.commit()

            order2 = Order(platform_order_id="DUP-001", platform=Platform.SHOPEE)
            session.add(order2)
            with pytest.raises(Exception):
                session.commit()

    def test_order_items_relationship(self, sample_order):
        with Session(engine) as session:
            order = session.get(Order, sample_order)
            item = OrderItem(
                order_id=order.id,
                platform_sku="SKU-001",
                product_name="手机壳",
                quantity=2,
                unit_price=150.0,
                unit_cost=80.0,
                subtotal=300.0,
            )
            session.add(item)
            session.commit()

            assert len(order.items) == 1
            assert order.items[0].product_name == "手机壳"


class TestProfitCalculator:
    """利润计算测试"""

    def test_calculate_profit(self, sample_order):
        from profit import calculator
        with Session(engine) as session:
            order = session.get(Order, sample_order)
            profit, margin = calculator.calculate_order_profit(order)
            # 450 - 200 - 30 - 60 - 10 = 150
            assert profit == 150.0
            # 150 / 450 = 0.3333
            assert margin == pytest.approx(0.3333, abs=0.001)

    def test_update_order_profit(self, sample_order):
        from profit import calculator
        order = calculator.update_order_profit(sample_order)
        assert order.profit == 150.0
        assert order.profit_margin == pytest.approx(0.3333, abs=0.001)

    def test_generate_report(self, sample_order):
        from profit import calculator
        report = calculator.generate_profit_report(
            period="daily",
            start_date=datetime.utcnow() - timedelta(days=1),
            end_date=datetime.utcnow() + timedelta(days=1),
        )
        assert report is not None
        assert report.period == "daily"


class TestAlertMonitor:
    """告警监控测试"""

    def test_check_overdue_ship(self, setup_db):
        with Session(engine) as session:
            order = Order(
                platform_order_id="OVERDUE-001",
                platform=Platform.SHOPEE,
                status=OrderStatus.PAID,
                paid_at=datetime.utcnow() - timedelta(hours=72),
                actual_amount=500.0,
                currency="TWD",
            )
            session.add(order)
            session.commit()
            order_id = order.id

        from alerts import monitor
        alerts = monitor.check_overdue_ship()
        assert len(alerts) >= 1
        assert alerts[0].alert_type == AlertType.OVERDUE_SHIP

    def test_check_low_profit(self, setup_db):
        with Session(engine) as session:
            order = Order(
                platform_order_id="LOWPROFIT-001",
                platform=Platform.SHOPEE,
                status=OrderStatus.COMPLETED,
                actual_amount=100.0,
                cost_of_goods=95.0,
                platform_fee=5.0,
                logistics_cost=5.0,
                profit=-5.0,
                profit_margin=-0.05,
            )
            session.add(order)
            session.commit()

        from alerts import monitor
        alerts = monitor.check_low_profit()
        assert len(alerts) >= 1
        assert alerts[0].alert_type == AlertType.LOW_PROFIT

    def test_resolve_alert(self, setup_db):
        with Session(engine) as session:
            alert = Alert(
                order_id=1,
                alert_type=AlertType.OVERDUE_SHIP,
                severity="warning",
                message="测试告警",
            )
            session.add(alert)
            session.commit()
            alert_id = alert.id

        from alerts import monitor
        resolved = monitor.resolve_alert(alert_id, "已处理")
        assert resolved.is_resolved is True
        assert resolved.resolved_note == "已处理"


class TestLogisticsTracker:
    """物流跟踪测试"""

    def test_add_tracking(self, sample_order):
        from logistics import tracker as t
        record = t.add_tracking(sample_order, "TN123456789", "shopee_express")
        assert record.tracking_number == "TN123456789"
        assert record.carrier == "shopee_express"

    def test_query_mock_tracking(self):
        from logistics import tracker as t
        result = t._mock_query("TN123456789")
        assert result["tracking_number"] == "TN123456789"
        assert "checkpoints" in result
        assert len(result["checkpoints"]) > 0
