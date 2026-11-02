"""
调度层单测：重点覆盖「连续消失才删」。

**用假 state 而不是真 Redis：** 要测的是判定逻辑（缺席几轮、URL 还在不在 sitemap、
删除接口是否确认），不是 Redis 本身；``CrawlState`` 的读写另有 ``test_state.py`` 。
删除信号用 monkeypatch 替换成协程，避免真打接入 API。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import crawler.main as main
from crawler.config import CrawlConfig


class FakeState:
    """
    只实现 ``_sweep_missing`` 用到的状态方法。

    :param seen: 初始 ``crawl:seen`` 。
    :param urls: ``external_id`` → 归一化 URL 映射。
    :param misses: 预置的缺席计数（模拟「上一轮已经缺席过」）。
    """

    def __init__(
        self,
        *,
        seen: set[str],
        urls: dict[str, str],
        misses: dict[str, int] | None = None,
    ) -> None:
        self._seen = set(seen)
        self._urls = dict(urls)
        self.misses = dict(misses or {})
        self.forgotten: list[tuple[str, str]] = []

    async def seen_ids(self) -> set[str]:
        """返回当前已见身份（复制一份）。"""
        return set(self._seen)

    async def id_url(self, external_id: str) -> str | None:
        """查身份对应的 URL。"""
        return self._urls.get(external_id)

    async def bump_miss(self, external_id: str) -> int:
        """缺席计数 +1 并返回。"""
        self.misses[external_id] = self.misses.get(external_id, 0) + 1
        return self.misses[external_id]

    async def forget(self, external_id: str, url: str) -> None:
        """记录并清掉一个身份的全部本地状态。"""
        self.forgotten.append((external_id, url))
        self._seen.discard(external_id)
        self._urls.pop(external_id, None)
        self.misses.pop(external_id, None)


@pytest.fixture
def cfg() -> CrawlConfig:
    """固定缺席阈值，别受测试容器环境变量影响。"""
    return replace(CrawlConfig.from_env(), miss_limit=2)


def _fake_send_delete(result: bool | None):
    """
    造一个假的 ``send_delete`` ，记录调用参数。

    :param result: 每次调用返回的值。
    :return: ``(协程函数, 调用记录列表)`` 。
    """
    calls: list[str] = []

    async def fake(client, *, cfg, state, external_id) -> bool | None:
        calls.append(external_id)
        return result

    return fake, calls


GONE = "https://fastapi.tiangolo.com/zh/gone/"
SITEMAP = {"https://fastapi.tiangolo.com/zh/alive/"}


# ---------- 连续消失 ----------


async def test_first_miss_round_does_not_delete(cfg, monkeypatch):
    """第一轮缺席只累计到 1（阈值 2）：不发删除、不 forget。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE})
    fake_delete, calls = _fake_send_delete(True)
    monkeypatch.setattr(main, "send_delete", fake_delete)

    counts = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)

    assert counts == {"deleted": 0, "missing": 1, "errors": 0}
    assert calls == []
    assert state.forgotten == []
    assert state.misses == {GONE: 1}


async def test_second_miss_round_deletes_and_forgets(cfg, monkeypatch):
    """第二轮仍缺席 → 达到阈值，调用删除并忘掉状态。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE})
    fake_delete, calls = _fake_send_delete(True)
    monkeypatch.setattr(main, "send_delete", fake_delete)

    first = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)
    second = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)

    assert first == {"deleted": 0, "missing": 1, "errors": 0}
    assert second == {"deleted": 1, "missing": 0, "errors": 0}
    assert calls == [GONE]
    assert state.forgotten == [(GONE, GONE)]
    assert GONE not in await state.seen_ids()


async def test_url_still_in_sitemap_is_not_missed(cfg, monkeypatch):
    """URL 还在 sitemap 里（只是本轮抓取失败）→ 不计缺席、不删。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE})
    fake_delete, calls = _fake_send_delete(True)
    monkeypatch.setattr(main, "send_delete", fake_delete)

    counts = await main._sweep_missing(state, {GONE}, set(), cfg, client=None)

    assert counts == {"deleted": 0, "missing": 0, "errors": 0}
    assert calls == []
    assert state.misses == {}


async def test_present_ids_are_skipped(cfg, monkeypatch):
    """本轮已处理成功的身份直接跳过，不累计缺席。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE})
    fake_delete, calls = _fake_send_delete(True)
    monkeypatch.setattr(main, "send_delete", fake_delete)

    counts = await main._sweep_missing(state, SITEMAP, {GONE}, cfg, client=None)

    assert counts == {"deleted": 0, "missing": 0, "errors": 0}
    assert state.misses == {}
    assert calls == []


async def test_delete_failure_keeps_state_for_retry(cfg, monkeypatch):
    """删除接口返回 ``None`` → 不 forget、计 errors；下一轮仍会重试删除。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE}, misses={GONE: 1})
    failing, fail_calls = _fake_send_delete(None)
    monkeypatch.setattr(main, "send_delete", failing)

    failed = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)

    assert failed == {"deleted": 0, "missing": 0, "errors": 1}
    assert fail_calls == [GONE]
    assert state.forgotten == []
    assert state.misses == {GONE: 2}          # 计数保留，下一轮继续够阈值

    succeeding, ok_calls = _fake_send_delete(True)
    monkeypatch.setattr(main, "send_delete", succeeding)

    retried = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)

    assert retried == {"deleted": 1, "missing": 0, "errors": 0}
    assert ok_calls == [GONE]
    assert state.forgotten == [(GONE, GONE)]


async def test_delete_false_is_terminal(cfg, monkeypatch):
    """接口 200 + ``deleted=false`` （本来就不在）也算终态：forget 并计 deleted。"""
    state = FakeState(seen={GONE}, urls={GONE: GONE}, misses={GONE: 1})
    fake_delete, calls = _fake_send_delete(False)
    monkeypatch.setattr(main, "send_delete", fake_delete)

    counts = await main._sweep_missing(state, SITEMAP, set(), cfg, client=None)

    assert counts == {"deleted": 1, "missing": 0, "errors": 0}
    assert calls == [GONE]
    assert state.forgotten == [(GONE, GONE)]


# ---------- 纯函数 ----------


def test_content_digest_ignores_whitespace():
    """换行 / 缩进差异不该改变正文哈希。"""
    assert main._content_digest("第一段\n\n第二段  第三段") == main._content_digest(
        "第一段 第二段 第三段"
    )


def test_storage_url_rejects_cross_host_canonical():
    """跨 host 的 canonical 不采信，退回本轮页面 URL。"""
    page = "https://fastapi.tiangolo.com/zh/doc/"
    assert main._storage_url(page, "https://evil.example/doc/") == page
    assert main._storage_url(page, "https://fastapi.tiangolo.com/zh/doc/") == page
    assert main._storage_url(page, "https://fastapi.tiangolo.com/zh/doc/") == page
