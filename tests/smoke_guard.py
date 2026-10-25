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


async def test_unlimited_user_bypasses_budget(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(
        main_module,
        db_path,
        daily_budget=0.01,
        unlimited_user_ids=["10001"],
    )
    event = FakeEvent(sender_id="10001")
    await plugin.guard_llm_request(event, FakeRequest())
    print("unlimited stopped:", event.stopped, "sent:", event.sent)
    assert not event.stopped
    assert not event.sent
    print("UNLIMITED USER OK")


async def test_websearch_block(main_module) -> None:
    db_path = create_usage_db({FAKE_PROVIDER_ID: (1, 0, 1)})
    plugin = make_plugin(
        main_module,
        db_path,
        daily_budget=2.0,
        websearch_price_per_1000=5000.0,
        enforce_chat=False,
    )
    _start, _end, day_key = plugin._day_start()
    await plugin._increment_websearch("deepseek", day_key, "group")
    report = await plugin._build_report()
    print("total spent:", report.total_spent, "budget:", report.total_cap)
    assert report.is_over_budget()

    event = FakeEvent()
    request = FakeRequest()
    await plugin.guard_llm_request(event, request)
    print("search removal sent:", event.sent, "stopped:", event.stopped)
    assert not event.stopped, "chat should continue when enforce_chat is off"
    assert event.sent, "search notice should be sent"
    assert "web_search_deepseek" not in request.func_tool.tools
    assert "deepseek_fetch_web_page" not in request.func_tool.tools
    assert "unrelated_tool" in request.func_tool.tools
    print("WEBSEARCH BLOCK OK")


async def test_over_budget_notice_cap(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(
        main_module,
        db_path,
        daily_budget=0.01,
        max_over_budget_notices=2,
    )
    event = FakeEvent()
    event.unified_msg_origin = "test:GroupMessage:notice-cap"
    for _ in range(4):
        event.stopped = False
        await plugin.guard_llm_request(event, FakeRequest())
        assert event.stopped
    print("notice count:", len(event.sent))
    assert len(event.sent) == 2, "only the configured number of notices is sent"
    print("NOTICE CAP OK")


async def test_peak_block(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(
        main_module,
        db_path,
        daily_budget=100.0,
        peak_block_enabled=True,
        peak_windows=["00:00-23:59"],
        peak_weekdays=[1, 2, 3, 4, 5, 6, 7],
        holidays=[],
    )
    event = FakeEvent()
    await plugin.guard_llm_request(event, FakeRequest())
    print("peak sent:", event.sent, "stopped:", event.stopped)
    assert event.stopped
    assert event.sent and "高峰" in str(event.sent[0])
    print("PEAK BLOCK OK")


async def test_whitelist_private_rows_excluded(main_module) -> None:
    db_path = create_usage_db(EXPENSIVE_TOKENS)
    plugin = make_plugin(
        main_module,
        db_path,
        unlimited_user_ids=["abc"],
        exclude_whitelist_billing=True,
    )
    rows = [
        {"umo": "p:FriendMessage:abc"},
        {"umo": "p:FriendMessage:xyz"},
        {"umo": "p:GroupMessage:g1"},
    ]
    kept = plugin._filter_whitelisted_private_rows(rows)
    print("kept rows:", kept)
    assert len(kept) == 2
    assert all(row["umo"] != "p:FriendMessage:abc" for row in kept)
    print("WHITELIST BILLING FILTER OK")


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
    await test_unlimited_user_bypasses_budget(main_module)
    await test_websearch_block(main_module)
    await test_over_budget_notice_cap(main_module)
    await test_peak_block(main_module)
    await test_whitelist_private_rows_excluded(main_module)
    await test_no_block_when_affordable(main_module)
    print("GUARD INTEGRATION OK")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except ImportError as exc:
        print("SKIP: AstrBot is not importable:", exc)
        sys.exit(0)
