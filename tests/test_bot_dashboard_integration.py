"""
봇 → 대시보드 캐시 무효화 연동 테스트.

대시보드 인증이 Basic 에서 Bearer 토큰으로 바뀌었을 때 봇 쪽이 함께
갱신되지 않아, 봇으로 기록해도 대시보드가 최대 수 분간 옛 숫자를 보여
주는 상태였습니다. 게다가 실패가 warning 으로 삼켜져 드러나지 않았습니다.

여기서는 실제 대시보드 앱을 ASGI 로 띄우고, 봇의 HTTP 호출을 그쪽으로
연결해 '정말로 캐시가 비워지는지' 를 확인합니다.
"""
import asyncio
import json
import urllib.error

import pytest

import budget_bot


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def dashboard(monkeypatch):
    """봇의 urlopen 을 실제 대시보드 앱(ASGI)으로 연결한다."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as dash

    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport

    calls = []

    def fake_urlopen(req, data=None, timeout=None):
        path = req.full_url.replace("http://dash", "")
        headers = {k.lower(): v for k, v in req.header_items()}
        calls.append({"path": path, "headers": headers})

        async def go():
            async with AsyncClient(transport=ASGITransport(app=dash.app),
                                   base_url="http://dash") as c:
                return await c.post(path, content=data, headers=headers)

        r = asyncio.run(go())
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, r.text, {}, None)
        return _Resp(r.content)

    monkeypatch.setattr(budget_bot.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(budget_bot, "DASHBOARD_URL", "http://dash")
    monkeypatch.setattr(budget_bot, "DASHBOARD_PASS", dash.DASH_PASS)
    monkeypatch.setattr(budget_bot, "_dash_token", None)
    return dash, calls


def test_bot_actually_clears_dashboard_cache(dashboard):
    """봇이 기록한 뒤 대시보드 응답 캐시가 실제로 비워져야 한다."""
    dash, calls = dashboard
    dash._dash_cache[(2026, 1)] = {"stale": True}
    dash._annual_cache[2026] = ["stale"]
    dash._trend_cache[(2026, 1, 6)] = ["stale"]

    budget_bot._clear_dashboard_cache_sync()

    assert len(dash._dash_cache) == 0, "대시보드 캐시가 비워지지 않았습니다"
    assert len(dash._annual_cache) == 0
    assert len(dash._trend_cache) == 0


def test_uses_bearer_and_csrf_header(dashboard):
    """Basic 이 아니라 Bearer 토큰 + CSRF 헤더를 보내야 한다."""
    dash, calls = dashboard
    budget_bot._clear_dashboard_cache_sync()

    auth_call, clear_call = calls[0], calls[1]
    assert auth_call["path"] == "/api/auth"
    assert clear_call["path"] == "/api/cache/clear"
    assert clear_call["headers"]["authorization"].startswith("Bearer ")
    assert "basic" not in clear_call["headers"]["authorization"].lower()
    assert clear_call["headers"]["x-dashboard-clear"] == "1"


def test_token_is_reused_across_calls(dashboard):
    """매번 로그인하지 않고 발급받은 토큰을 재사용해야 한다."""
    dash, calls = dashboard
    budget_bot._clear_dashboard_cache_sync()
    budget_bot._clear_dashboard_cache_sync()
    auth_calls = [c for c in calls if c["path"] == "/api/auth"]
    assert len(auth_calls) == 1, f"토큰을 매번 재발급했습니다 ({len(auth_calls)}회)"


def test_expired_token_triggers_reauth(dashboard):
    """토큰이 만료되면 재발급 후 한 번 더 시도해야 한다."""
    dash, calls = dashboard
    budget_bot._clear_dashboard_cache_sync()
    # 서버에서 토큰을 모두 무효화 (만료 상황)
    dash._tokens.clear()
    dash._dash_cache[(2026, 2)] = {"stale": True}

    budget_bot._clear_dashboard_cache_sync()

    assert len(dash._dash_cache) == 0, "만료 후 재인증이 동작하지 않았습니다"
    assert len([c for c in calls if c["path"] == "/api/auth"]) == 2


def test_wrong_password_does_not_crash_the_bot(dashboard, monkeypatch):
    """대시보드 인증이 실패해도 봇 기록 흐름은 계속되어야 한다."""
    dash, _ = dashboard
    monkeypatch.setattr(budget_bot, "DASHBOARD_PASS", "틀린비밀번호")
    monkeypatch.setattr(budget_bot, "_dash_token", None)
    asyncio.run(budget_bot._clear_dashboard_cache())     # 예외가 새면 실패


def test_skipped_when_not_configured(monkeypatch):
    """DASHBOARD_URL 이 없으면 아무 요청도 하지 않는다."""
    monkeypatch.setattr(budget_bot, "DASHBOARD_URL", "")
    called = {"n": 0}
    monkeypatch.setattr(budget_bot, "_clear_dashboard_cache_sync",
                        lambda: called.__setitem__("n", called["n"] + 1))
    asyncio.run(budget_bot._clear_dashboard_cache())
    assert called["n"] == 0
