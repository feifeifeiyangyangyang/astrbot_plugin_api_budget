"""AstrBot plugin: per-API-key daily spending budget with in-chat reporting."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # relative import when loaded as a package by AstrBot
    from .budget import (
        DEFAULT_PEAK_WEEKDAYS,
        DEFAULT_PEAK_WINDOWS,
        DEFAULT_PRICING_JSON,
        DEFAULT_TIMEZONE,
        DEFAULT_WEBSEARCH_TOOLS,
        DayCost,
        Report,
        UsageDataUnavailable,
        aggregate_by_day,
        compute_session_report,
        find_usage_database,
        fmt_money,
        format_detail,
        format_history,
        format_today,
        local_day_range,
        normalize_pricing,
        parse_peak_config,
        query_usage,
        resolve_timezone,
    )
except ImportError:  # direct import, e.g. unit tests
    from budget import (  # type: ignore[no-redef]
        DEFAULT_PEAK_WEEKDAYS,
        DEFAULT_PEAK_WINDOWS,
        DEFAULT_PRICING_JSON,
        DEFAULT_TIMEZONE,
        DEFAULT_WEBSEARCH_TOOLS,
        DayCost,
        Report,
        UsageDataUnavailable,
        aggregate_by_day,
        compute_session_report,
        find_usage_database,
        fmt_money,
        format_detail,
        format_history,
        format_today,
        local_day_range,
        normalize_pricing,
        parse_peak_config,
        query_usage,
        resolve_timezone,
    )

PLUGIN_NAME = "astrbot_plugin_api_budget"
STATE_VERSION = 1
DEFAULT_BLOCK_MESSAGE = "🐟 今日 API 额度已用完啦（{key}：{spent} / {budget}），明天再来找我玩吧～"
DEFAULT_HISTORY_DAYS = 7
MAX_HISTORY_DAYS = 30


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ApiBudgetPlugin(Star):
    """Enforce a daily CNY budget per AstrBot API key and answer /额度."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._state_lock = asyncio.Lock()
        try:
            self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:  # pragma: no cover - only when runtime paths are missing
            self._data_dir = Path(__file__).resolve().parent / "data"
            self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._data_dir / "state.json"
        self._state = self._load_state()
        self.logger.info(
            "API 额度管家已加载，用量数据库：%s",
            self._usage_db_path() or "未找到",
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        if self._usage_db_path() is None:
            self.logger.warning(
                "未找到 AstrBot 用量数据库（data/data_v4.db）。"
                "本插件需要 AstrBot >= 4.23 且使用本地 Agent 才能统计用量。"
            )

    async def terminate(self) -> None:
        self._save_state()

    # ------------------------------------------------------------------
    # settings helpers
    # ------------------------------------------------------------------

    def _setting(self, key: str, default):
        try:
            if not self.config:
                return default
            value = self.config.get(key, default)
            return default if value is None else value
        except Exception:
            return default

    def _main_config(self) -> dict:
        try:
            config = self.context.get_config()
            return config if isinstance(config, dict) else {}
        except Exception:
            return {}

    def _reset_hour(self) -> int:
        try:
            hour = int(self._setting("reset_hour", 0))
        except (TypeError, ValueError):
            hour = 0
        return min(max(hour, 0), 23)

    def _history_days(self) -> int:
        try:
            days = int(self._setting("history_days", DEFAULT_HISTORY_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_HISTORY_DAYS
        return min(max(days, 1), MAX_HISTORY_DAYS)

    def _timezone(self):
        name = str(
            self._setting("timezone", "") or self._main_config().get("timezone") or DEFAULT_TIMEZONE
        )
        return resolve_timezone(name)

    def _usage_db_path(self) -> Path | None:
        override = str(self._setting("db_path", "") or "").strip()
        if override:
            return Path(override).expanduser()
        data_root = self._data_dir.parent.parent
        return find_usage_database(data_root)

    def _websearch_provider(self) -> str:
        settings = self._main_config().get("provider_settings") or {}
        if not isinstance(settings, dict):
            return ""
        return str(settings.get("websearch_provider") or "").strip()

    def _websearch_tool_names(self) -> set[str]:
        configured = self._setting("websearch_tool_names", None)
        names = set(DEFAULT_WEBSEARCH_TOOLS)
        if isinstance(configured, str):
            names.update(item.strip() for item in configured.split(",") if item.strip())
        elif isinstance(configured, (list, tuple, set)):
            names.update(str(item).strip() for item in configured if str(item).strip())
        return names

    # ------------------------------------------------------------------
    # small persistent state (websearch counters + notice cooldowns)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", STATE_VERSION)
                data.setdefault("websearch", {})
                data.setdefault("websearch_scope", {})
                data.setdefault("notice", {})
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:  # corrupted file should never crash the bot
            logger.warning("读取额度插件状态失败：%s", exc)
        return {
            "version": STATE_VERSION,
            "websearch": {},
            "websearch_scope": {},
            "notice": {},
        }

    def _save_state(self) -> None:
        try:
            self._state["version"] = STATE_VERSION
            temp_path = self._state_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self._state_path)
        except Exception as exc:  # pragma: no cover - disk issues
            self.logger.warning("保存额度插件状态失败：%s", exc)

    def _websearch_count(self, day_key: str, provider: str) -> int:
        """Legacy global counter (kept for state migration)."""
        if not provider:
            return 0
        try:
            counter = self._state.get("websearch", {}).get(day_key, {})
            return int(counter.get(provider, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    def _websearch_scope_counts(self, day_key: str, provider: str) -> dict[str, int]:
        result = {"group": 0, "private": 0}
        if not provider:
            return result
        try:
            day_counter = self._state.get("websearch_scope", {}).get(day_key, {})
            provider_counter = (
                day_counter.get(provider, {}) if isinstance(day_counter, dict) else {}
            )
            if isinstance(provider_counter, dict):
                for scope in result:
                    result[scope] = int(provider_counter.get(scope, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            pass
        # Old versions only stored a global count. Attribute it to group chats
        # so existing data is not silently dropped.
        result["group"] += self._websearch_count(day_key, provider)
        return result

    async def _increment_websearch(self, provider: str, day_key: str, scope: str) -> None:
        async with self._state_lock:
            counters = self._state.setdefault("websearch_scope", {})
            day_counter = counters.setdefault(day_key, {})
            provider_counter = day_counter.setdefault(provider, {"group": 0, "private": 0})
            provider_counter[scope] = int(provider_counter.get(scope, 0) or 0) + 1
            self._trim_state(day_key)
            self._save_state()

    def _trim_state(self, day_key: str) -> None:
        try:
            keep_after = date.fromisoformat(day_key) - timedelta(days=13)
        except ValueError:
            return
        for state_key in ("websearch", "websearch_scope"):
            counters = self._state.get(state_key, {})
            for key in list(counters):
                try:
                    if date.fromisoformat(key) < keep_after:
                        counters.pop(key, None)
                except ValueError:
                    counters.pop(key, None)
        notices = self._state.get("notice", {})
        cutoff = time.time() - 30 * 86400
        for key in list(notices):
            try:
                if float(notices[key]) < cutoff:
                    notices.pop(key, None)
            except (TypeError, ValueError):
                notices.pop(key, None)

    def _is_unlimited_user(self, event: AstrMessageEvent) -> bool:
        configured = self._setting("unlimited_user_ids", [])
        if isinstance(configured, str):
            ids = [item.strip() for item in configured.split(",") if item.strip()]
        elif isinstance(configured, (list, tuple, set)):
            ids = [str(item).strip() for item in configured if str(item).strip()]
        else:
            ids = []
        return bool(ids) and str(event.get_sender_id()) in ids

    async def _allow_notice(self, key: str, cooldown_seconds: int) -> bool:
        async with self._state_lock:
            notices = self._state.setdefault("notice", {})
            now = time.time()
            try:
                last = float(notices.get(key, 0) or 0)
            except (TypeError, ValueError):
                last = 0.0
            if now - last < cooldown_seconds:
                return False
            notices[key] = now
            self._save_state()
            return True

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def _day_start(self) -> tuple[datetime, datetime, str]:
        tz = self._timezone()
        start, end = local_day_range(datetime.now(tz), tz, self._reset_hour())
        return start, end, start.astimezone(tz).date().isoformat()

    def _pricing(self):
        return normalize_pricing(self._setting("pricing_json", DEFAULT_PRICING_JSON))

    def _peak_config(self):
        return parse_peak_config(
            self._setting("peak_windows", DEFAULT_PEAK_WINDOWS),
            self._setting("peak_weekdays", DEFAULT_PEAK_WEEKDAYS),
            self._setting("holidays", []),
        )

    def _budget_settings(self) -> float:
        """Return the daily total budget in CNY; 0 means unlimited."""
        try:
            return float(self._setting("daily_budget", 1.0))
        except (TypeError, ValueError):
            return 1.0

    async def _build_report(self) -> Report:
        tz = self._timezone()
        start, end, day_key = self._day_start()
        date_label = f"{start.astimezone(tz):%Y-%m-%d}"
        db_path = self._usage_db_path()
        if db_path is None:
            return Report(
                date_label=date_label,
                available=False,
                error="找不到 AstrBot 用量数据库（data/data_v4.db）。",
            )
        try:
            rows = await asyncio.to_thread(query_usage, db_path, start, end)
        except UsageDataUnavailable as exc:
            return Report(date_label=date_label, available=False, error=str(exc))

        provider = self._websearch_provider()
        total_budget = self._budget_settings()
        try:
            websearch_price = float(self._setting("websearch_price_per_1000", 0.0))
        except (TypeError, ValueError):
            websearch_price = 0.0

        search_counts = self._websearch_scope_counts(day_key, provider) if provider else {}
        return compute_session_report(
            rows,
            search_counts,
            pricing=self._pricing(),
            tz=tz,
            peak_config=self._peak_config(),
            group_budget=0.0,
            private_budget=0.0,
            websearch_price_per_1000=websearch_price,
            date_label=date_label,
            total_budget=total_budget,
        )

    async def _build_history(self, days: int):
        tz = self._timezone()
        today_start, _end, _day_key = self._day_start()
        query_start = today_start - timedelta(days=days - 1)
        query_end = datetime.now(tz)
        db_path = self._usage_db_path()
        if db_path is None:
            raise UsageDataUnavailable("找不到 AstrBot 用量数据库。")
        rows = await asyncio.to_thread(query_usage, db_path, query_start, query_end)
        today = today_start.astimezone(tz).date()
        day_costs = aggregate_by_day(
            rows,
            pricing=self._pricing(),
            tz=tz,
            peak_config=self._peak_config(),
        )
        # Include counted web-search calls in the daily totals.
        provider = self._websearch_provider()
        try:
            search_price = float(self._setting("websearch_price_per_1000", 0.0))
        except (TypeError, ValueError):
            search_price = 0.0
        if provider and search_price > 0:
            for offset in range(days):
                day = today - timedelta(days=offset)
                count = sum(self._websearch_scope_counts(day.isoformat(), provider).values())
                if count:
                    bucket = day_costs.setdefault(day, DayCost(day=day))
                    bucket.cost += count * search_price / 1000.0
        return day_costs, today

    # ------------------------------------------------------------------
    # budget enforcement
    # ------------------------------------------------------------------

    def _render_block_message(self, report: Report, group) -> str:
        template = (
            str(self._setting("block_message", DEFAULT_BLOCK_MESSAGE) or "").strip()
            or DEFAULT_BLOCK_MESSAGE
        )
        values = {
            "key": group.label,
            "budget": fmt_money(group.budget),
            "spent": fmt_money(group.spent),
            "remaining": fmt_money(max(group.remaining, 0.0)),
            "date": report.date_label,
            "total_spent": fmt_money(report.total_spent),
            "total_budget": fmt_money(report.total_budget),
        }
        try:
            return template.format_map(_SafeFormatDict(values))
        except Exception:
            return template

    def _disable_websearch_tools(self, req) -> list[str]:
        toolset = getattr(req, "func_tool", None)
        if toolset is None:
            return []
        names = set(self._websearch_tool_names())
        if self._setting("remove_all_search_tools", True):
            try:
                for name in toolset.names():
                    lowered = str(name).lower()
                    if (
                        lowered.startswith("web_search_")
                        or "fetch_web_page" in lowered
                        or lowered.endswith("_web_search")
                        or lowered == "exa_get_contents"
                    ):
                        names.add(str(name))
            except Exception:
                pass
        removed: list[str] = []
        for name in names:
            try:
                if toolset.get_tool(name) is not None:
                    toolset.remove_tool(name)
                    removed.append(name)
            except Exception:
                continue
        return removed

    @filter.on_llm_request()
    async def guard_llm_request(self, event: AstrMessageEvent, req) -> None:
        """Block the request when the relevant budget bucket is exhausted."""
        try:
            if not bool(self._setting("enabled", True)):
                return
            if self._is_unlimited_user(event):
                return
            enforce_chat = bool(self._setting("enforce_chat", True))
            enforce_websearch = bool(self._setting("enforce_websearch", True))
            if not enforce_chat and not enforce_websearch:
                return
            report = await self._build_report()
            if not report.available:
                return
            scope = "private" if event.is_private_chat() else "group"
            scope_key = f"scope:{scope}"
            if enforce_chat:
                blockers = report.blockers(scope_key)
                if blockers:
                    text = self._render_block_message(report, blockers[0])
                    await event.send(event.plain_result(text))
                    event.stop_event()
                    return
            if enforce_websearch and report.websearch_over_budget(scope_key):
                removed = self._disable_websearch_tools(req)
                if removed and bool(self._setting("websearch_notice", True)):
                    if await self._allow_notice(f"websearch:{event.unified_msg_origin}", 3600):
                        await event.send(
                            event.plain_result("🔎 联网搜索额度已用完，本轮已自动关闭搜索。")
                        )
        except Exception:
            self.logger.warning("额度拦截检查失败（已跳过，不影响正常对话）", exc_info=True)

    @filter.on_llm_tool_respond()
    async def count_websearch_tool(
        self, event: AstrMessageEvent, tool, tool_args, tool_result
    ) -> None:
        """Count successful web-search tool calls so their cost can be shown."""
        try:
            name = str(getattr(tool, "name", "") or "")
            if name not in self._websearch_tool_names():
                return
            if not bool(self._setting("count_failed_searches", False)):
                if bool(getattr(tool_result, "isError", False)):
                    return
            provider = self._websearch_provider()
            if not provider:
                return
            _start, _end, day_key = self._day_start()
            scope = "private" if event.is_private_chat() else "group"
            await self._increment_websearch(provider, day_key, scope)
        except Exception:
            self.logger.warning("统计联网搜索调用失败", exc_info=True)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @filter.command("额度", alias={"花费", "apikey", "用量", "额度查询"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_budget(self, event: AstrMessageEvent):
        """查看今天各 API Key 的花费和剩余额度"""
        report = await self._build_report()
        yield event.plain_result(format_today(report, self._reset_hour()))

    @filter.command("额度明细", alias={"花费明细"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_budget_detail(self, event: AstrMessageEvent):
        """查看今天按模型拆分的花费明细"""
        report = await self._build_report()
        yield event.plain_result(format_detail(report))

    @filter.command("额度历史", alias={"花费历史"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_budget_history(self, event: AstrMessageEvent):
        """查看最近几天的每日花费"""
        days = self._history_days()
        match = re.search(r"(?:额度历史|花费历史)\s*(\d{1,2})", event.message_str or "")
        if match:
            days = min(max(int(match.group(1)), 1), MAX_HISTORY_DAYS)
        try:
            day_costs, today = await self._build_history(days)
            text = format_history(day_costs, today, days)
        except UsageDataUnavailable as exc:
            text = f"📊 历史花费暂不可用：{exc}"
        except Exception:
            self.logger.warning("查询历史花费失败", exc_info=True)
            text = "📊 查询历史花费失败，请稍后再试。"
        yield event.plain_result(text)
