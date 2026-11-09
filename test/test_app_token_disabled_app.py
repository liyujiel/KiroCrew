"""An app token is accepted only while its app is installed and enabled.

The token is HMAC-signed with the gateway key, so its signature alone stays
valid after the owner disables or uninstalls the app. These tests run a real
app token through the real ``token_auth_middleware`` against a real
``installed.json`` record and pin that:

* a disabled or uninstalled app's token is refused the way an invalid token is;
* a token minted before a disable stays refused after the app is enabled again,
  while a fresh exchange works;
* an enabled app's token and a dashboard token are unaffected.

One injected clock drives both the token mint and the record write, so the
order of "minted" and "disabled" is set by the test, never by timer resolution.
"""

from __future__ import annotations

import contextlib
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager_records import InstalledApp, _write_installed, app_dir
from kiro_crew.dashboard import revocation_gen, token_auth

_APP = "token-app"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def module(self) -> types.ModuleType:
        fake = types.ModuleType("fake_time")
        fake.__dict__.update(time.__dict__)
        fake.time = lambda: self.now  # type: ignore[attr-defined]
        return fake

    def tick(self) -> None:
        self.now += 1.0


@pytest.fixture
def clock(tmp_path, monkeypatch) -> _Clock:
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.apps.manager_records.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"app-token-disabled-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    c = _Clock()
    fake = c.module()
    monkeypatch.setattr("kiro_crew.dashboard.token_auth_tokens.time", fake)
    monkeypatch.setattr("kiro_crew.apps.manager_records.time", fake)
    return c


def _set_enabled(clock: _Clock, enabled: bool) -> None:
    clock.tick()
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=enabled))
    clock.tick()


def _mint(clock: _Clock) -> str:
    clock.tick()
    return token_auth.generate_token(_APP, app=_APP)


def _valid(token: str) -> bool:
    return token_auth.validate_token_with_app(token)[0]


def test_a_disabled_apps_token_is_refused_like_an_invalid_one(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    assert _valid(token)

    _set_enabled(clock, False)
    assert token_auth.validate_token_with_app(token) == (False, "", "invalid signature", "")


def test_an_uninstalled_apps_token_is_refused(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    (app_dir(_APP) / "installed.json").unlink()

    assert not _valid(token)


def test_a_token_from_before_a_disable_stays_dead_after_reenable(clock) -> None:
    _set_enabled(clock, True)
    old = _mint(clock)
    _set_enabled(clock, False)
    _set_enabled(clock, True)

    assert not _valid(old)
    assert _valid(_mint(clock))


def test_an_enabled_app_and_a_dashboard_token_are_unaffected(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    _set_enabled(clock, True)

    assert _valid(token)
    assert token_auth.validate_token_with_app(token_auth.generate_token("owner"))[:2] == (
        True,
        "owner",
    )


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@contextlib.asynccontextmanager
async def _client():
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app.router.add_get("/api/apps/{name}/manifest", _ok)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_middleware_refuses_a_disabled_apps_token_on_its_own_route(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    async with _client() as client:

        async def status(tok: str) -> int:
            resp = await client.get(f"/api/apps/{_APP}/manifest", params={"token": tok})
            return resp.status

        assert await status(token) == 200
        _set_enabled(clock, False)
        assert await status(token) == 403
        _set_enabled(clock, True)
        assert await status(token) == 403
        assert await status(_mint(clock)) == 200
