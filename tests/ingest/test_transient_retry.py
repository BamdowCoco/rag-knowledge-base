"""瞬时错误重试：死锁（1213）/ 锁等待超时（1205）回滚后重跑，其它错误不重试。"""
import pytest
from sqlalchemy.exc import OperationalError

from app.db.retry import with_transient_retry
from app.services.document_service import DocumentService


class FakeSession:
    """只记录 rollback 次数 —— 单测不碰真库。"""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _operational(code: int) -> OperationalError:
    return OperationalError("INSERT ...", {}, Exception(code, "test"))


async def test_retries_deadlock_then_succeeds():
    session = FakeSession()
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _operational(1213)
        return "ok"

    result = await with_transient_retry(flaky, session=session, base_delay=0)

    assert result == "ok"
    assert calls == 2
    assert session.rollbacks == 1      # 重试前回滚了一次


async def test_gives_up_after_attempts():
    session = FakeSession()
    calls = 0

    async def always_deadlock():
        nonlocal calls
        calls += 1
        raise _operational(1213)

    with pytest.raises(OperationalError):
        await with_transient_retry(
            always_deadlock, session=session, attempts=3, base_delay=0
        )

    assert calls == 3
    assert session.rollbacks == 3


async def test_non_transient_error_is_not_retried():
    session = FakeSession()
    calls = 0

    async def broken():
        nonlocal calls
        calls += 1
        raise _operational(1146)       # 表不存在 —— 不是瞬时错误

    with pytest.raises(OperationalError):
        await with_transient_retry(broken, session=session, base_delay=0)

    assert calls == 1
    assert session.rollbacks == 1


async def test_ingest_retries_deadlock_and_still_returns_201(client, make_doc, monkeypatch):
    """端到端：身份路径第一次死锁、第二次成功 → 请求仍返回 201。"""
    original = DocumentService._apply_identified_in_tx
    calls = 0

    async def flaky(self, raw, cleaned, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _operational(1213)
        return await original(self, raw, cleaned, **kwargs)

    monkeypatch.setattr(DocumentService, "_apply_identified_in_tx", flaky)

    r = await client.post("/api/v1/documents", json=make_doc())

    assert r.status_code == 201
    assert calls == 2
