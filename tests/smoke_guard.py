"""Optional smoke test: exercise budget enforcement with a temporary database.

Prerequisite: the real AstrBot package must be importable (set ``PYTHONPATH``
to the AstrBot source directory).  No real bot data is touched.

Example (Windows, AstrBot venv):

    set PYTHONPATH=E:\\AsrBot\\instances\\<id>\\core
    E:\\AsrBot\\instances\\<id>\\venv\\Scripts\\python.exe tests\\smoke_guard.py
"""

from __future__ import annotations

import asyncio
import sys

from _smoke_utils import (
    FAKE_PROVIDER_CONFIG,
    FAKE_PROVIDER_ID,
    FakeContext,
    FakeEvent,
    FakeRequest,
    create_usage_db,
    load_plugin_modules,
    plugin_config,
    prepare_runtime,
)

EXPENSIVE_TOKENS = {FAKE_PROVIDER_ID: (1_000_000, 0, 1_000_000)}


def make_plugin(main_module, db_path, **overrides):
    return main_module.ApiBudgetPlugin(
        FakeContext(FAKE_PROVIDER_CONFIG), plugin_config(db_path, **overrides)
    )


async def test_chat_block(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(main_module, db_path, daily_budget=0.01)
    event = FakeEvent()
    request = FakeRequest()
    await plugin.guard_llm_request(event, request)
    print("chat block sent:", event.sent, "stopped:", event.stopped)
    assert event.stopped, "request should be stopped when over budget"
    assert event.sent, "block message should be sent"
    assert "额度" in str(event.sent[0])
    assert "web_search_deepseek" in request.func_tool.tools
    print("CHAT BLOCK OK")


async def test_websearch_block(main_module) -> None:
    db_path = create_usage_db({FAKE_PROVIDER_ID: (1, 0, 1)})
    plugin = make_plugin(
        main_module,
        db_path,
        daily_budget=2.0,
        websearch_price_per_1000=5000.0,
    )
    _start, _end, day_key = plugin._day_start()
    await plugin._increment_websearch("deepseek", day_key)
    report = await plugin._build_report()
    search_group = report.websearch_group()
    print(
        "websearch group spent:",
        search_group.spent if search_group else None,
        "budget:",
        search_group.budget if search_group else None,
    )
    assert search_group is not None and search_group.over_budget

    event = FakeEvent()
    request = FakeRequest()
    await plugin.guard_llm_request(event, request)
    print("search removal sent:", event.sent, "stopped:", event.stopped)
    assert not event.stopped, "chat should continue when only search is over budget"
    assert event.sent, "search notice should be sent"
    assert "web_search_deepseek" not in request.func_tool.tools
    assert "deepseek_fetch_web_page" not in request.func_tool.tools
    assert "unrelated_tool" in request.func_tool.tools
    print("WEBSEARCH BLOCK OK")


async def test_no_block_when_affordable(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(main_module, db_path, daily_budget=100.0)
    event = FakeEvent()
    request = FakeRequest()
    await plugin.guard_llm_request(event, request)
    print("no block sent:", event.sent, "stopped:", event.stopped)
    assert not event.stopped
    assert not event.sent
    print("NO BLOCK OK")


async def run() -> None:
    prepare_runtime()
    main_module, _budget_module = load_plugin_modules()
    await test_chat_block(main_module)
    await test_websearch_block(main_module)
    await test_no_block_when_affordable(main_module)
    print("GUARD INTEGRATION OK")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except ImportError as exc:
        print("SKIP: AstrBot is not importable:", exc)
        sys.exit(0)
