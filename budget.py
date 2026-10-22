"""Cost accounting helpers for the API budget plugin.

This module is intentionally free of any AstrBot imports so it can be unit
tested on its own.  It turns the per-request token rows that AstrBot stores in
the ``provider_stats`` table into CNY cost, grouped by the API key that paid
for them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEZONE = "Asia/Shanghai"
_CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# DeepSeek published prices, CNY per 1M tokens (2026-10).
# They can be overridden from the plugin settings; this is only a fallback.
DEFAULT_PRICING: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "peak": {"cached_input": 0.04, "input": 2.0, "output": 8.0},
        "off_peak": {"cached_input": 0.02, "input": 1.0, "output": 4.0},
    },
    "deepseek-v4-pro": {
        "peak": {"cached_input": 0.30, "input": 9.0, "output": 27.0},
        "off_peak": {"cached_input": 0.15, "input": 4.5, "output": 13.5},
    },
    "default": {
        "peak": {"cached_input": 0.04, "input": 2.0, "output": 8.0},
        "off_peak": {"cached_input": 0.02, "input": 1.0, "output": 4.0},
    },
}

DEFAULT_PRICING_JSON = json.dumps(DEFAULT_PRICING, ensure_ascii=False, indent=2)

DEFAULT_PEAK_WINDOWS = ["09:00-12:00", "14:00-18:00"]
DEFAULT_PEAK_WEEKDAYS = [1, 2, 3, 4, 5]

# Built-in AstrBot web-search / web-fetch tools.  Each successful call is
# billed per request by the search provider, so the plugin counts them.
DEFAULT_WEBSEARCH_TOOLS = [
    "web_search_tavily",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
    "web_search_baidu",
    "web_search_exa",
    "web_search_anysearch",
    "web_search_deepseek",
    "web_search_serpapi",
    "tavily_extract_web_page",
    "firecrawl_extract_web_page",
    "deepseek_fetch_web_page",
    "exa_get_contents",
]


class UsageDataUnavailable(RuntimeError):
    """Raised when the AstrBot usage database cannot be read."""


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------


def resolve_timezone(name: str | None) -> timezone | Any:
    """Best effort conversion of a timezone name to a tzinfo object."""
    if not name:
        return _CN_TZ
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        fallback = {
            "Asia/Shanghai": _CN_TZ,
            "Asia/Chongqing": _CN_TZ,
            "Asia/Urumqi": timezone(timedelta(hours=6), "Asia/Urumqi"),
            "UTC": timezone.utc,
            "Etc/UTC": timezone.utc,
        }
        return fallback.get(name, _CN_TZ)


def local_day_range(now: datetime, tz: Any, reset_hour: int = 0) -> tuple[datetime, datetime]:
    """Return the [start, end) aware datetimes of the current "budget day"."""
    local_now = now.astimezone(tz)
    start = datetime.combine(local_now.date(), time(hour=reset_hour), tzinfo=tz)
    if local_now < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)


def _utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _sqlite_utc_string(dt: datetime) -> str:
    return _utc_naive(dt).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sqlite_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class PeakConfig:
    windows: tuple[tuple[time, time], ...]
    weekdays: frozenset[int]
    holidays: frozenset[str]

    def is_peak(self, local_dt: datetime) -> bool:
        if local_dt.strftime("%Y-%m-%d") in self.holidays:
            return False
        if local_dt.isoweekday() not in self.weekdays:
            return False
        current = local_dt.time()
        return any(start <= current < end for start, end in self.windows)


def parse_time_windows(raw: Any) -> tuple[tuple[time, time], ...]:
    windows: list[tuple[time, time]] = []
    for item in raw or []:
        if not isinstance(item, str) or "-" not in item:
            continue
        left, right = item.split("-", 1)
        try:
            start = datetime.strptime(left.strip(), "%H:%M").time()
            end = datetime.strptime(right.strip(), "%H:%M").time()
        except ValueError:
            continue
        if start < end:
            windows.append((start, end))
    return tuple(windows)


def parse_peak_config(
    windows: Any = None,
    weekdays: Any = None,
    holidays: Any = None,
) -> PeakConfig:
    parsed_windows = parse_time_windows(DEFAULT_PEAK_WINDOWS if windows is None else windows)
    weekday_set: set[int] = set()
    for item in weekdays if weekdays is not None else DEFAULT_PEAK_WEEKDAYS:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 7:
            weekday_set.add(value)
    holiday_set = {
        str(item) for item in (holidays or []) if isinstance(item, str) and len(item) >= 10
    }
    return PeakConfig(
        windows=parsed_windows,
        weekdays=frozenset(weekday_set),
        holidays=frozenset(holiday_set),
    )


def _row_local_datetime(row: dict[str, Any], tz: Any) -> datetime:
    end_time = _to_float(row.get("end_time"))
    if end_time > 0:
        return datetime.fromtimestamp(end_time, tz)
    created = _parse_sqlite_utc(row.get("created_at"))
    if created is not None:
        return created.astimezone(tz)
    return datetime.now(tz)


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    cached_input: float = 0.0
    input_miss: float = 0.0
    output: float = 0.0


PriceTable = dict[str, dict[str, Price]]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_from_mapping(raw: Any) -> Price:
    if not isinstance(raw, dict):
        return Price()
    return Price(
        cached_input=_to_float(
            raw.get("cached_input", raw.get("input_cached", raw.get("cache_hit")))
        ),
        input_miss=_to_float(raw.get("input", raw.get("input_miss", raw.get("input_other")))),
        output=_to_float(raw.get("output")),
    )


def _normalize_entry(raw: Any) -> dict[str, Price]:
    if isinstance(raw, dict):
        peak_raw = raw.get("peak")
        off_raw = raw.get("off_peak", raw.get("offpeak", raw.get("idle")))
        if isinstance(peak_raw, dict) or isinstance(off_raw, dict):
            return {
                "peak": _price_from_mapping(peak_raw),
                "off_peak": _price_from_mapping(off_raw),
            }
        flat = _price_from_mapping(raw)
        return {"peak": flat, "off_peak": flat}
    return {"peak": Price(), "off_peak": Price()}


def normalize_pricing(raw: Any) -> PriceTable:
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    table: PriceTable = {}
    for model, entry in data.items():
        table[str(model).strip().lower()] = _normalize_entry(entry)
    for fallback_model, fallback_entry in DEFAULT_PRICING.items():
        table.setdefault(fallback_model, _normalize_entry(fallback_entry))
    return table


def lookup_price(table: PriceTable, model: str | None) -> tuple[dict[str, Price], bool]:
    """Return pricing for a model and whether it came from an exact match."""
    name = (model or "").strip().lower()
    if name and name in table:
        return table[name], True
    # Tolerate version/date suffixes such as "deepseek-flash-1017".
    candidates = [key for key in table if key not in {"default"} and key and key in name]
    if candidates:
        return table[max(candidates, key=len)], True
    return table.get("default", {"peak": Price(), "off_peak": Price()}), False


# ---------------------------------------------------------------------------
# usage database
# ---------------------------------------------------------------------------


def find_usage_database(data_dir: str | Path) -> Path | None:
    """Locate the AstrBot SQLite database inside the data directory."""
    root = Path(data_dir)
    preferred = root / "data_v4.db"
    if preferred.exists():
        return preferred
    candidates = sorted(root.glob("data_v*.db"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def query_usage(db_path: str | Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Read provider_stats rows whose usage belongs to [start, end)."""
    path = Path(db_path)
    if not path.is_file():
        raise UsageDataUnavailable(f"找不到 AstrBot 数据库：{path}")
    start_epoch = start.timestamp()
    end_epoch = end.timestamp()
    start_text = _sqlite_utc_string(start)
    end_text = _sqlite_utc_string(end)
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(
                "SELECT provider_id, provider_model, "
                "token_input_other, token_input_cached, token_output, "
                "start_time, end_time, created_at FROM provider_stats "
                "WHERE (end_time > 0 AND end_time >= ? AND end_time < ?) "
                "OR (end_time <= 0 AND created_at >= ? AND created_at < ?) "
                "ORDER BY id ASC",
                (start_epoch, end_epoch, start_text, end_text),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "no such table" in message:
            raise UsageDataUnavailable(
                "当前 AstrBot 版本没有 provider_stats 表，无法统计用量。"
            ) from exc
        raise UsageDataUnavailable(f"读取用量数据库失败：{message}") from exc


# ---------------------------------------------------------------------------
# API key discovery
# ---------------------------------------------------------------------------


def mask_key(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-4:]}"


def _as_key_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _provider_sources(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = config.get("provider_sources") or {}
    if isinstance(sources, list):
        return {
            str(item.get("id")): item
            for item in sources
            if isinstance(item, dict) and item.get("id")
        }
    if isinstance(sources, dict):
        return {str(key): value for key, value in sources.items() if isinstance(value, dict)}
    return {}


def _pretty_provider(source_id: str) -> str:
    mapping = {
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "gemini": "Gemini",
        "google": "Gemini",
        "moonshot": "Moonshot",
        "moonshotai": "Moonshot",
        "zhipu": "智谱",
        "bailian": "百炼",
        "dashscope": "百炼",
        "siliconflow": "硅基流动",
        "openrouter": "OpenRouter",
        "ollama": "Ollama",
        "mimo": "MiMo",
    }
    key = (source_id or "").strip()
    return mapping.get(key.lower(), key or "未知提供商")


@dataclass(frozen=True)
class KeyRef:
    """Identity of the API key that paid for a usage row."""

    key_id: str
    masked: str
    label: str
    source_ids: tuple[str, ...]
    kind: str  # "chat" or "websearch"


def build_key_map(
    config: dict[str, Any], websearch_tool_provider: str | None = None
) -> tuple[dict[str, KeyRef], KeyRef | None]:
    """Map AstrBot provider ids to the API key they belong to.

    AstrBot 4.29 stores LLM keys in ``provider_sources`` and lets several
    providers share one source key.  The usage database only stores the
    provider id, so this mapping is what turns "deepseek/deepseek-flash" into
    "the DeepSeek key".
    """
    sources = _provider_sources(config)
    chat_map: dict[str, KeyRef] = {}
    providers = config.get("provider") or []
    if not isinstance(providers, list):
        providers = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue
        provider_type = str(provider.get("provider_type") or "").strip().lower()
        if provider_type in {"text_to_speech", "speech_to_text", "embedding"}:
            continue
        source_id = str(
            provider.get("provider_source_id") or provider.get("provider_source") or ""
        ).strip()
        source = sources.get(source_id, {}) if source_id else {}
        if not source and not source_id:
            source = provider
            source_id = provider_id
        raw_keys = _as_key_list(source.get("key")) or _as_key_list(source.get("api_key"))
        if not raw_keys:
            raw_keys = _as_key_list(provider.get("api_key"))
        raw_key = raw_keys[0] if raw_keys else provider_id
        label_source = source_id or provider_id
        label = f"{_pretty_provider(label_source)} 聊天 Key"
        if raw_keys:
            label += f"（{mask_key(raw_key)}）"
        chat_map[provider_id] = KeyRef(
            key_id=raw_key,
            masked=mask_key(raw_keys[0]) if raw_keys else "",
            label=label,
            source_ids=tuple(item for item in (source_id, provider_id) if item),
            kind="chat",
        )

    websearch_ref: KeyRef | None = None
    settings = config.get("provider_settings") or {}
    if isinstance(settings, dict):
        provider_name = str(
            websearch_tool_provider or settings.get("websearch_provider") or ""
        ).strip()
        if provider_name:
            key_candidates: list[str] = []
            key_candidates.extend(_as_key_list(settings.get(f"websearch_{provider_name}_key")))
            if provider_name == "baidu_ai_search":
                key_candidates.extend(_as_key_list(settings.get("websearch_baidu_app_builder_key")))
            raw_key = key_candidates[0] if key_candidates else ""
            label = f"联网搜索（{_pretty_provider(provider_name)}"
            if raw_key:
                label += f"，{mask_key(raw_key)}"
            label += "）"
            websearch_ref = KeyRef(
                key_id=f"websearch:{provider_name}",
                masked=mask_key(raw_key) if raw_key else "",
                label=label,
                source_ids=(f"websearch:{provider_name}",),
                kind="websearch",
            )
    return chat_map, websearch_ref


def parse_budget_overrides(raw: Any) -> dict[str, float]:
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        return {}
    overrides: dict[str, float] = {}
    for key, value in data.items():
        try:
            overrides[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return overrides


def resolve_budget(ref: KeyRef, default_budget: float, overrides: dict[str, float]) -> float:
    for candidate in (*ref.source_ids, ref.key_id, ref.masked):
        if candidate and candidate in overrides:
            return overrides[candidate]
    return default_budget


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    cached_input: int = 0
    input_other: int = 0
    output: int = 0
    count: int = 0
    cost: float = 0.0
    peak_cost: float = 0.0
    unknown_price: bool = False

    @property
    def total_tokens(self) -> int:
        return self.cached_input + self.input_other + self.output

    @property
    def input_tokens(self) -> int:
        return self.cached_input + self.input_other

    def add(self, other: Usage) -> None:
        self.cached_input += other.cached_input
        self.input_other += other.input_other
        self.output += other.output
        self.count += other.count
        self.cost += other.cost
        self.peak_cost += other.peak_cost
        self.unknown_price = self.unknown_price or other.unknown_price


@dataclass
class GroupReport:
    key_id: str
    label: str
    kind: str
    budget: float
    usage: Usage = field(default_factory=Usage)
    models: dict[str, Usage] = field(default_factory=dict)

    @property
    def spent(self) -> float:
        return self.usage.cost

    @property
    def remaining(self) -> float:
        return self.budget - self.usage.cost

    @property
    def over_budget(self) -> bool:
        return self.budget > 0 and self.usage.cost >= self.budget


@dataclass
class Report:
    date_label: str
    groups: list[GroupReport] = field(default_factory=list)
    available: bool = True
    error: str = ""
    websearch_count: int = 0
    budget_mode: str = "per_key"
    shared_budget: float = 0.0
    shared_spent: float = 0.0

    @property
    def shared(self) -> bool:
        return self.budget_mode == "shared"

    @property
    def total_spent(self) -> float:
        return sum(group.spent for group in self.groups)

    @property
    def total_budget(self) -> float:
        if self.shared:
            return self.shared_budget
        return sum(group.budget for group in self.groups if group.budget > 0)

    @property
    def total_remaining(self) -> float:
        if self.total_budget <= 0:
            return 0.0
        return self.total_budget - self.total_spent

    @property
    def shared_over(self) -> bool:
        return self.shared and self.shared_budget > 0 and self.shared_spent >= self.shared_budget

    @property
    def has_unknown_price(self) -> bool:
        return any(group.usage.unknown_price for group in self.groups)

    def is_over_budget(self) -> bool:
        if self.shared:
            return self.shared_over
        return any(group.over_budget for group in self.groups)

    def blockers(self, key_id: str | None = None) -> list[GroupReport]:
        if self.shared:
            if not self.shared_over:
                return []
            # A shared budget covers every chat key, so any chat request is
            # blocked once the combined spend reaches the limit.
            return [group for group in self.groups if group.kind == "chat"]
        result = []
        for group in self.groups:
            if group.kind != "chat" or not group.over_budget:
                continue
            if key_id is not None and group.key_id != key_id:
                continue
            result.append(group)
        return result

    def websearch_over_budget(self) -> bool:
        if self.shared:
            return self.shared_over
        group = self.websearch_group()
        return bool(group and group.over_budget)

    def websearch_group(self) -> GroupReport | None:
        for group in self.groups:
            if group.kind == "websearch":
                return group
        return None


def _usage_from_row(row: dict[str, Any]) -> Usage:
    return Usage(
        cached_input=int(_to_float(row.get("token_input_cached"))),
        input_other=int(_to_float(row.get("token_input_other"))),
        output=int(_to_float(row.get("token_output"))),
        count=1,
    )


def compute_report(
    rows: list[dict[str, Any]],
    chat_map: dict[str, KeyRef],
    websearch_ref: KeyRef | None,
    websearch_count: int,
    *,
    pricing: PriceTable,
    tz: Any,
    peak_config: PeakConfig,
    default_budget: float,
    overrides: dict[str, float],
    websearch_price_per_1000: float,
    date_label: str,
    budget_mode: str = "per_key",
) -> Report:
    shared = budget_mode == "shared"
    report = Report(
        date_label=date_label,
        websearch_count=websearch_count,
        budget_mode="shared" if shared else "per_key",
    )
    groups: dict[str, GroupReport] = {}

    def get_group(ref: KeyRef) -> GroupReport:
        group = groups.get(ref.key_id)
        if group is None:
            group = GroupReport(
                key_id=ref.key_id,
                label=ref.label,
                kind=ref.kind,
                budget=0.0 if shared else resolve_budget(ref, default_budget, overrides),
            )
            groups[ref.key_id] = group
        return group

    for row in rows:
        provider_id = str(row.get("provider_id") or "unknown")
        ref = chat_map.get(provider_id)
        if ref is None:
            ref = KeyRef(
                key_id=provider_id,
                masked="",
                label=f"未知来源（{provider_id}）",
                source_ids=(provider_id,),
                kind="chat",
            )
        group = get_group(ref)
        model = str(row.get("provider_model") or "未知模型")
        price_pair, exact = lookup_price(pricing, model)
        peak = peak_config.is_peak(_row_local_datetime(row, tz))
        price = price_pair["peak"] if peak else price_pair["off_peak"]
        usage = _usage_from_row(row)
        usage.cost = (
            usage.cached_input * price.cached_input
            + usage.input_other * price.input_miss
            + usage.output * price.output
        ) / 1_000_000
        usage.peak_cost = usage.cost if peak else 0.0
        usage.unknown_price = not exact
        group.usage.add(usage)
        model_usage = group.models.setdefault(model, Usage())
        model_usage.add(usage)

    if websearch_ref is not None:
        group = get_group(websearch_ref)
        usage = Usage(count=websearch_count)
        usage.cost = (
            websearch_count * websearch_price_per_1000 / 1000.0
            if websearch_price_per_1000 > 0
            else 0.0
        )
        group.usage.add(usage)

    report.groups = sorted(groups.values(), key=lambda item: (item.kind != "chat", item.label))
    if shared:
        report.shared_spent = sum(group.spent for group in groups.values())
        report.shared_budget = max(default_budget, 0.0)
    return report


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def fmt_money(value: float) -> str:
    if value == 0:
        return "¥0.00"
    if abs(value) < 0.01:
        return f"¥{value:.4f}"
    return f"¥{value:.2f}"


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _group_header(group: GroupReport, shared: bool = False) -> str:
    icon = "🔑" if group.kind == "chat" else "🔎"
    if shared:
        budget_text = fmt_money(group.spent)
    elif group.budget > 0:
        budget_text = f"{fmt_money(group.spent)} / {fmt_money(group.budget)}"
    else:
        budget_text = f"{fmt_money(group.spent)}（未设预算）"
    line = f"{icon} {group.label}：{budget_text}"
    if not shared and group.over_budget:
        line += " ⚠️ 已超限"
    return line


def format_today(report: Report, reset_hour: int = 0) -> str:
    if not report.available:
        return (
            "📊 API 额度统计暂不可用\n"
            f"{report.error}\n"
            "请确认 AstrBot 版本 >= 4.23 且使用本地 Agent。"
        )
    lines = [f"💰 今日 API 花费 · {report.date_label}", ""]
    if report.shared:
        if report.shared_budget > 0:
            lines.append(
                f"总计：{fmt_money(report.total_spent)} / {fmt_money(report.shared_budget)}"
            )
            lines.append(
                f"（聊天 + 联网搜索共用，剩余 {fmt_money(max(report.total_remaining, 0.0))}）"
            )
        else:
            lines.append(f"总计：{fmt_money(report.total_spent)}（共用额度，未设预算）")
    else:
        lines.append(f"总计：{fmt_money(report.total_spent)}")
    lines.append("")

    if not report.groups:
        lines.append("今天还没有产生 API 调用。")
    for group in report.groups:
        lines.append(_group_header(group, report.shared))
        if group.kind == "websearch":
            lines.append(f"   搜索调用：{group.usage.count} 次")
        else:
            usage = group.usage
            lines.append(
                "   输入 "
                f"{fmt_tokens(usage.input_tokens)}"
                f"（缓存 {fmt_tokens(usage.cached_input)}）"
                f" · 输出 {fmt_tokens(usage.output)}"
                f" · 调用 {usage.count} 次"
            )
        if group.usage.unknown_price:
            lines.append("   ⚠️ 该模型未配置价格，按默认价估算")

    lines.append("")
    if report.has_unknown_price:
        lines.append("提示：在插件配置的「价格表」中补充模型价格会更准确。")
    lines.append(f"⏰ 每天 {reset_hour:02d}:00 重置额度（本地时区）")
    if report.is_over_budget():
        lines.append("⚠️ 已达每日额度，机器人会暂停回答，明天自动恢复。")
    return "\n".join(lines)


def format_detail(report: Report) -> str:
    if not report.available:
        return f"📊 明细暂不可用：{report.error}"
    lines = [f"📊 今日 API 明细 · {report.date_label}"]
    if report.shared:
        lines.append("（聊天 + 联网搜索共用额度）")
    if not report.groups:
        lines.append("今天还没有产生 API 调用。")
        return "\n".join(lines)
    for group in report.groups:
        lines.append("")
        lines.append(_group_header(group, report.shared))
        if group.kind == "websearch":
            lines.append(f"   搜索调用：{group.usage.count} 次")
            continue
        if not group.models:
            continue
        for model, usage in sorted(
            group.models.items(), key=lambda item: item[1].cost, reverse=True
        ):
            peak_cost = usage.peak_cost
            off_peak_cost = usage.cost - peak_cost
            lines.append(
                f"   {model}：{usage.count} 次 · "
                f"输入 {fmt_tokens(usage.input_tokens)}"
                f"（缓存 {fmt_tokens(usage.cached_input)}）· "
                f"输出 {fmt_tokens(usage.output)} · "
                f"{fmt_money(usage.cost)}"
                f"（高峰 {fmt_money(peak_cost)} / "
                f"空闲 {fmt_money(off_peak_cost)}）"
            )
    return "\n".join(lines)


@dataclass
class DayCost:
    day: date
    cost: float = 0.0
    count: int = 0


def aggregate_by_day(
    rows: list[dict[str, Any]],
    *,
    pricing: PriceTable,
    tz: Any,
    peak_config: PeakConfig,
) -> dict[date, DayCost]:
    result: dict[date, DayCost] = {}
    for row in rows:
        local_dt = _row_local_datetime(row, tz)
        day = local_dt.date()
        bucket = result.setdefault(day, DayCost(day=day))
        model = str(row.get("provider_model") or "")
        price_pair, _exact = lookup_price(pricing, model)
        price = price_pair["peak"] if peak_config.is_peak(local_dt) else price_pair["off_peak"]
        usage = _usage_from_row(row)
        bucket.cost += (
            usage.cached_input * price.cached_input
            + usage.input_other * price.input_miss
            + usage.output * price.output
        ) / 1_000_000
        bucket.count += 1
    return result


def format_history(day_costs: dict[date, DayCost], today: date, days: int) -> str:
    days = max(1, min(days, 30))
    lines = [f"📅 最近 {days} 天花费（截至 {today:%Y-%m-%d}）", ""]
    max_cost = max(
        (
            day_costs.get(today - timedelta(days=index), DayCost(day=today)).cost
            for index in range(days)
        ),
        default=0.0,
    )
    total = 0.0
    has_data = False
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        bucket = day_costs.get(day, DayCost(day=day))
        total += bucket.cost
        if bucket.cost > 0:
            has_data = True
        bar = ""
        if max_cost > 0 and bucket.cost > 0:
            width = max(1, round(bucket.cost / max_cost * 10))
            bar = " " + "█" * width
        lines.append(f"{day:%m-%d}{bar}  {fmt_money(bucket.cost)}")
    lines.append("")
    if has_data:
        lines.append(f"合计：{fmt_money(total)}")
        lines.append(f"日均：{fmt_money(total / days)}")
    else:
        lines.append("这段时间没有产生 API 调用。")
    return "\n".join(lines)
