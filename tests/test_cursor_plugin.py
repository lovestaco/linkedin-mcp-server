"""Contract tests for the Cursor plugin package."""

from __future__ import annotations

import json
from pathlib import Path

from test_codex_plugin import _assert_mcp_contract, _load_json, _project_version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "linkedin-mcp-server"
_CURSOR_MANIFEST = _PLUGIN_ROOT / ".cursor-plugin" / "plugin.json"
_CURSOR_MARKETPLACE = _REPO_ROOT / ".cursor-plugin" / "marketplace.json"
_PLUGIN_MCP = _PLUGIN_ROOT / ".mcp.json"
_README = _REPO_ROOT / "README.md"
_DEEPLINK_PREFIX = "https://cursor.com/en/install-mcp?name=linkedin&config="


def test_cursor_manifest_matches_the_repository_release() -> None:
    manifest = _load_json(_CURSOR_MANIFEST)
    assert manifest["name"] == _PLUGIN_ROOT.name
    assert manifest["version"] == _project_version()
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert manifest["logo"] == "assets/icon.svg"
    assert "variables" not in manifest
    assert "enabled" not in json.dumps(manifest)


def test_marketplace_points_at_the_plugin_directory() -> None:
    marketplace = _load_json(_CURSOR_MARKETPLACE)
    assert marketplace["name"] == "linkedin-mcp-server"
    assert marketplace["owner"]["name"] == "Daniel Sticker"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry["name"] == "linkedin-mcp-server"
    assert entry["source"] == "plugins/linkedin-mcp-server"


def test_cursor_shares_the_plugin_mcp_config() -> None:
    _assert_mcp_contract(_load_json(_PLUGIN_MCP))


