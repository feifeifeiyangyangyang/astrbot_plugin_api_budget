"""Shared helpers for the optional integration smoke tests.

These tests need the real AstrBot package on ``PYTHONPATH`` (see README):
they load the plugin exactly like AstrBot does, but they build their own
temporary SQLite database and fake platform objects, so they never touch a
real bot's data.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

FAKE_PROVIDER_CONFIG = {
    "provider_sources": {
        "deepseek": {
            "id": "deepseek",
            "type": "openai_chat_completion",
            "key": ["sk-fake-1234567890abcdef"],
        }
    },
    "provider": [
        {
            "id": "deepseek/deepseek-flash",
            "provider_source_id": "deepseek",
            "model": "deepseek-flash",
        }
    ],
    "provider_settings": {
        "websearch_provider": "deepseek",
        "websearch_deepseek_key": ["sk-fake-search-123456"],
    },
    "timezone": "Asia/Shanghai",
}

FAKE_PROVIDER_ID = "deepseek/deepseek-flash"
FAKE_PROVIDER_SOURCE = "deepseek"


def prepare_runtime() -> None:
    """Isolate AstrBot paths and make sure local imports work."""
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot-budget-test-")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_plugin_modules():
    """Import the plugin as a package (relative imports) plus its budget module."""
    package_name = "astrbot_plugin_api_budget"
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name,
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        assert spec and spec.loader
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        spec.loader.exec_module(package)
    main_module = importlib.import_module(f"{package_name}.main")
    budget_module = importlib.import_module(f"{package_name}.budget")
    return main_module, budget_module


def create_usage_db(
    tokens: dict[str, tuple[int, int, int]] | None = None,
    umo: str = "test:GroupMessage:1",
) -> Path:
    """Create a temp provider_stats database.

    ``tokens`` maps provider_id -> (uncached input, cached input, output).
    """
    tokens = tokens or {FAKE_PROVIDER_ID: (0, 0, 0)}
    db_path = Path(tempfile.mkdtemp(prefix="astrbot-budget-db-")) / "data_v4.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE provider_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT, "
        "provider_model TEXT, umo TEXT, token_input_other INTEGER, "
        "token_input_cached INTEGER, token_output INTEGER, "
        "start_time REAL, end_time REAL, created_at TEXT)"
    )
    now = datetime.now(timezone.utc)
    end_ts = now.timestamp()
    created = now.strftime("%Y-%m-%d %H:%M:%S")
    for provider_id, (input_other, cached, output) in tokens.items():
        connection.execute(
            "INSERT INTO provider_stats (provider_id, provider_model, umo, "
            "token_input_other, token_input_cached, token_output, "
            "start_time, end_time, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                provider_id,
                "deepseek-flash",
                umo,
                input_other,
                cached,
                output,
                end_ts,
                end_ts,
                created,
            ),
        )
    connection.commit()
    connection.close()
    return db_path


def plugin_config(db_path: Path, **overrides) -> dict:
    config = {
        "enabled": True,
        "group_daily_budget": 2.0,
        "private_daily_budget": 2.0,
        "db_path": str(db_path),
        "timezone": "Asia/Shanghai",
        "enforce_chat": True,
        "enforce_websearch": True,
        "websearch_notice": True,
        "remove_all_search_tools": True,
        "pricing_json": "",
    }
    config.update(overrides)
    return config


class FakeProvider:
    provider_config = {"id": FAKE_PROVIDER_ID}


class FakeContext:
    def __init__(self, config: dict):
        self._config = config

    async def get_using_provider_async(self, umo=None):
        return FakeProvider()

    def get_config(self, umo=None):
        return self._config


class FakeTool:
    def __init__(self, name: str):
        self.name = name


class FakeToolSet:
    def __init__(self):
        self.tools = {
            "web_search_deepseek": FakeTool("web_search_deepseek"),
            "deepseek_fetch_web_page": FakeTool("deepseek_fetch_web_page"),
            "unrelated_tool": FakeTool("unrelated_tool"),
        }

    def names(self):
        return list(self.tools)

    def get_tool(self, name):
        return self.tools.get(name)

    def remove_tool(self, name):
        self.tools.pop(name, None)


class FakeRequest:
    def __init__(self):
        self.func_tool = FakeToolSet()


class FakeEvent:
    def __init__(self, private: bool = False, sender_id: str = "10001"):
        self.private = private
        self.sender_id = sender_id
        kind = "FriendMessage" if private else "GroupMessage"
        self.unified_msg_origin = f"test:{kind}:1"
        self.message_str = "/额度"
        self.sent = []
        self.stopped = False

    def is_private_chat(self):
        return self.private

    def get_sender_id(self):
        return self.sender_id

    def plain_result(self, text):
        return text

    async def send(self, message):
        self.sent.append(message)

    def stop_event(self):
        self.stopped = True


def today_key(plugin, budget_module) -> tuple[str, datetime]:
    tz = budget_module.resolve_timezone("Asia/Shanghai")
    start, _end = budget_module.local_day_range(datetime.now(tz), tz, plugin._reset_hour())
    return start.astimezone(tz).date().isoformat(), start
