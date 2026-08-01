"""EventBus 单元测试 — 可单独运行：pytest tests/test_event_bus.py"""
import threading
import pytest
from agent_os.src.core.infra.event_bus import EventBus


class TestEventBus:
    def test_subscribe_publish(self):
        bus = EventBus()
        received = []
        bus.subscribe("run.event", lambda p: received.append(p))
        bus.publish("run.event", run_id="r1", event={"kind": "text"})
        assert len(received) == 1
        assert received[0]["run_id"] == "r1"

    def test_multiple_subscribers(self):
        bus = EventBus()
        hits_a, hits_b = [], []
        bus.subscribe("run.event", lambda p: hits_a.append(1))
        bus.subscribe("run.event", lambda p: hits_b.append(1))
        bus.publish("run.event", run_id="r1")
        assert len(hits_a) == 1
        assert len(hits_b) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        hits = []
        handler = lambda p: hits.append(1)
        bus.subscribe("run.dirty", handler)
        bus.unsubscribe("run.dirty", handler)
        bus.publish("run.dirty", run_id="r1")
        assert len(hits) == 0

    def test_no_subscribers_no_error(self):
        bus = EventBus()
        bus.publish("run.event", run_id="r1")

    def test_handler_exception_isolated(self):
        bus = EventBus()
        good_hits = []
        bus.subscribe("run.event", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe("run.event", lambda p: good_hits.append(1))
        bus.publish("run.event", run_id="r1")
        assert len(good_hits) == 1

    def test_thread_safety(self):
        bus = EventBus()
        counter = {"n": 0}
        lock = threading.Lock()
        def handler(p):
            with lock:
                counter["n"] += 1
        bus.subscribe("run.event", handler)
        threads = [threading.Thread(target=bus.publish, args=("run.event",), kwargs={"run_id": f"r{i}"}) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert counter["n"] == 50
