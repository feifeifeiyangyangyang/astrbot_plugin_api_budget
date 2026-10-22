"""Optional smoke test: load the plugin with AstrBot and render a report.

Prerequisite: the real AstrBot package must be importable (set ``PYTHONPATH``
to the AstrBot source directory).  The test builds its own temporary usage
database, so it does not read or modify the bot's real data.

Example (Windows, AstrBot venv):

    set PYTHONPATH=E:\\AsrBot\\instances\\<id>\\core
    E:\\AsrBot\\instances\\<id>\\venv\\Scripts\\python.exe tests\\smoke_plugin.py
"""

from __future__ import annotations

import asyncio
import sys

from _smoke_utils import (
    FAKE_PROVIDER_CONFIG,
    FAKE_PROVIDER_ID,
    FakeContext,
    FakeRequest,
    create_usage_db,
    load_plugin_modules,
    plugin_config,
    prepare_runtime,
)


async def main() -> None:
    prepare_runtime()
    main_module, budget_module = load_plugin_modules()

    db_path = create_usage_db({FAKE_PROVIDER_ID: (1_000_000, 0, 1_000_000)})
    plugin = main_module.ApiBudgetPlugin(FakeContext(FAKE_PROVIDER_CONFIG), plugin_config(db_path))
    report = await plugin._build_report()
    print("available:", report.available, "error:", report.error)
    print("date:", report.date_label, "groups:", len(report.groups))
    for group in report.groups:
        print(
            f"  - {group.label} | kind={group.kind} | spent={group.spent:.4f} "
            f"| budget={group.budget} | count={group.usage.count}"
        )
    print("total spent:", round(report.total_spent, 4))
    print()
    print("--- /额度 output ---")
    print(budget_module.format_today(report, 0))

    request = FakeRequest()
    removed = plugin._disable_websearch_tools(request)
    print()
    print("removed search tools:", sorted(removed))
    assert "web_search_deepseek" in removed
    assert "deepseek_fetch_web_page" in removed
    assert "unrelated_tool" in request.func_tool.tools

    assert report.available, report.error
    assert report.total_spent > 0, "expected the sample usage to cost money"
    assert report.groups, "expected at least one key group"
    print()
    print("SMOKE OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ImportError as exc:
        print("SKIP: AstrBot is not importable:", exc)
        sys.exit(0)
