"""Tests for SmartBox 7-inch KaiTi dashboard theme, font shards, and bootstrap CSS."""
from __future__ import annotations

from pathlib import Path
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.config as _cfg_mod
import hermes_cli.web_server as ws
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.dashboard_auth.middleware import _path_is_public
from hermes_cli.web_routers.dashboard_ui import _FONT_CHOICES, get_dashboard_themes
from hermes_cli.web_server_config import CONFIG_SCHEMA
from hermes_cli.web_server_dashboard import (
    _BUILTIN_DASHBOARD_THEMES,
    _normalise_theme_definition,
    _render_active_theme_bootstrap_css,
    mount_spa,
)
from hermes_cli.web_server_files import _dashboard_local_update_managed_externally
from hermes_cli.web_server_static_assets import is_static_asset_path, public_font_file


def test_default_dashboard_theme_is_smartbox_7inch():
    assert DEFAULT_CONFIG["dashboard"]["theme"] == "smartbox-7inch"


def test_builtin_themes_font_choices_and_config_schema_synced():
    builtin_names = [t["name"] for t in _BUILTIN_DASHBOARD_THEMES]
    assert builtin_names[:4] == [
        "smartbox-7inch",
        "smartbox-7inch-18",
        "smartbox-7inch-b",
        "smartbox-7inch-c",
    ]
    assert _BUILTIN_DASHBOARD_THEMES[0]["label"] == "智星盒 7″ · 方案A 大字 (20px)"
    assert "kaiti" in _FONT_CHOICES
    schema_options = list(CONFIG_SCHEMA["dashboard.theme"]["options"])
    assert schema_options[:4] == [
        "smartbox-7inch",
        "smartbox-7inch-18",
        "smartbox-7inch-b",
        "smartbox-7inch-c",
    ]
    assert set(schema_options) >= set(builtin_names)


def test_fonts_kai_is_public_and_resolves_without_path_traversal(tmp_path):
    shard = "fonts-kai/wenkai/lxgwwenkai-regular-subset-119.woff2"
    assert _path_is_public(f"/{shard}") is True
    assert is_static_asset_path(shard) is True

    empty_dist = tmp_path / "empty_dist"
    empty_dist.mkdir()
    resolved = public_font_file(empty_dist, empty_dist, shard)
    assert resolved is not None and resolved.is_file()
    assert resolved.suffix == ".woff2"

    # Traversal into an existing sibling font directory must fail closed.
    traversal = "fonts-kai/../fonts-terminal/JetBrainsMono-Regular.woff2"
    assert public_font_file(empty_dist, empty_dist, traversal) is None


def test_forwarded_prefix_rewrites_fonts_kai_in_css_and_index(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    dist = tmp_path / "web_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><head><link rel="preload" href="/fonts-kai/geist-sans-latin-400-normal.woff2"></head>'
        "<body>SPA</body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.css").write_text(
        '@font-face{font-family:"SmartBox Kai";'
        'src:url("/fonts-kai/wenkai/lxgwwenkai-regular-subset-119.woff2") format("woff2");}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ws, "WEB_DIST", dist)

    spa_app = FastAPI()
    mount_spa(spa_app)
    client = TestClient(spa_app)

    css_resp = client.get("/assets/app.css", headers={"X-Forwarded-Prefix": "/hermes"})
    assert css_resp.status_code == 200
    assert 'url("/hermes/fonts-kai/wenkai/lxgwwenkai-regular-subset-119.woff2")' in css_resp.text

    html_resp = client.get("/chat", headers={"X-Forwarded-Prefix": "/hermes"})
    assert html_resp.status_code == 200
    assert 'href="/hermes/fonts-kai/geist-sans-latin-400-normal.woff2"' in html_resp.text


def test_theme_bootstrap_css_handles_smartbox_15px_builtins_and_user_themes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "smartbox-7inch"}})
    assert _render_active_theme_bootstrap_css() == ""

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "smartbox-7inch-18"}})
    assert _render_active_theme_bootstrap_css() == (
        '<style id="hermes-theme-bootstrap">'
        ":root{--theme-base-size:18px;--theme-line-height:1.58;}"
        "</style>"
    )

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "smartbox-7inch-b"}})
    assert _render_active_theme_bootstrap_css() == (
        '<style id="hermes-theme-bootstrap">'
        ":root{--theme-base-size:22px;--theme-line-height:1.52;}"
        "</style>"
    )

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "smartbox-7inch-c"}})
    assert _render_active_theme_bootstrap_css() == (
        '<style id="hermes-theme-bootstrap">'
        ":root{--theme-base-size:24px;--theme-line-height:1.48;}"
        "</style>"
    )

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "default-large"}})
    assert _render_active_theme_bootstrap_css() == (
        '<style id="hermes-theme-bootstrap">'
        ":root{--theme-base-size:18px;--theme-line-height:1.65;}"
        "</style>"
    )

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"dashboard": {"theme": "default"}})
    bootstrap_15 = _render_active_theme_bootstrap_css()
    assert bootstrap_15 == (
        '<style id="hermes-theme-bootstrap">'
        ":root{--theme-base-size:15px;--theme-line-height:1.55;}"
        "</style>"
    )


def test_dashboard_local_update_managed_externally_for_opt_smartbox(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(sys, "prefix", "/opt/smartbox/hermes-venv")
    assert _dashboard_local_update_managed_externally() is True

    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(ws, "PROJECT_ROOT", Path("/opt/smartbox/hermes-agent"))
    assert _dashboard_local_update_managed_externally() is True


def test_normalise_theme_definition_preserves_and_clamps_terminal_metrics():
    normalised = _normalise_theme_definition({
        "name": "custom-touch",
        "terminalFontSize": 16,
        "terminalLineHeight": 1.18,
    })
    assert normalised is not None
    assert normalised["terminalFontSize"] == 16
    assert normalised["terminalLineHeight"] == 1.18
    assert '"SmartBox Kai"' in normalised["typography"]["fontSans"]
    assert '"SmartBox Kai"' in normalised["typography"]["fontMono"]

    clamped = _normalise_theme_definition({
        "name": "clamped",
        "terminalFontSize": 99,
        "terminalLineHeight": 0.4,
    })
    assert clamped is not None
    assert clamped["terminalFontSize"] == 32
    assert clamped["terminalLineHeight"] == 1.0

    rejected_bools = _normalise_theme_definition({
        "name": "bools",
        "terminalFontSize": True,
        "terminalLineHeight": False,
    })
    assert rejected_bools is not None
    assert "terminalFontSize" not in rejected_bools
    assert "terminalLineHeight" not in rejected_bools


@pytest.mark.asyncio
async def test_get_dashboard_themes_defaults_active_to_smartbox_7inch(monkeypatch):
    monkeypatch.setattr(_cfg_mod, "load_config", dict)
    resp = await get_dashboard_themes()
    assert resp["active"] == "smartbox-7inch"
