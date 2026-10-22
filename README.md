# astrbot_plugin_api_budget · API 额度管家

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

给 AstrBot 加一个「每日 API 花费上限」：按 API Key 统计每天花了多少人民币，
到上限自动停止回答，并支持在 QQ 里用 `/额度` 随时查询。

> 适合把机器人放在群里、又担心群友聊太嗨把 API 余额烧光的场景。

---

## 功能

- 💰 **按 API Key 统计每天花费**：读取 AstrBot 自己记录的 token 用量，区分
  「缓存命中 / 缓存未命中 / 输出」，按峰谷价算出人民币金额。
- 🚦 **每日预算拦截**：每个 Key 默认 2 元/天（可改），达到上限后不再调用模型，
  在群里回复提示（文案可自定义）。
- 🔎 **联网搜索也管**：统计搜索工具调用次数，超过搜索 Key 的预算后，
  自动从本轮请求里移除搜索工具（默认 DeepSeek 搜索 2026-11-01 前免费，价格可配）。
- 📊 **聊天内查询**（仅管理员）：
  - `/额度`：今天各 Key 花了多少、还剩多少、token 明细
  - `/额度明细`：按模型拆分，高峰/空闲花费分别显示
  - `/额度历史 7`：最近 N 天每日花费（1–30 天）
- ⚙️ **WebUI 配置**：价格表、预算、重置时间、高峰时段等都能在 AstrBot 面板里改。
- 🔒 **本地只读**：只读取本机 AstrBot 的 SQLite 用量表，不上传任何数据；
  密钥在输出中只显示前后几位（如 `sk-***d42d`）。

### 在 QQ 里的效果

```text
/额度
💰 今日 API 花费 · 2026-10-22

总计：¥0.23

🔑 DeepSeek 聊天 Key（sk-***d42d）：¥0.23 / ¥2.00
   输入 962.7k（缓存 853.4k） · 输出 13.3k · 调用 27 次
🔎 联网搜索（DeepSeek，sk-***3456）：¥0.00 / ¥2.00
   搜索调用：0 次

⏰ 每天 00:00 重置额度（本地时区）
```

---

## 工作原理

AstrBot 4.23+ 的本地 Agent 每完成一次回复，会把本次请求的 token 用量写进
`data/data_v4.db` 的 `provider_stats` 表（包含缓存命中、未命中的输入 token 和输出
token）。本插件：

1. 每次准备调用 LLM 前，读取「今天」的用量行；
2. 按 Key 聚合，套用价格表计算人民币花费；
3. 达到预算就终止这次请求并回复提示；
4. `/额度` 等指令也基于同一份数据生成。

因为用的是 AstrBot 自己记录的完整用量，即使一次对话里发生了多轮工具调用，
统计也不会漏。

### 默认价格（DeepSeek 官方，元 / 百万 token）

| 模型 | 时段 | 缓存命中输入 | 缓存未命中输入 | 输出 |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | 高峰 | 0.04 | 2.00 | 8.00 |
| `deepseek-flash` | 空闲 | 0.02 | 1.00 | 4.00 |
| `deepseek-v4-pro` | 高峰 | 0.30 | 9.00 | 27.00 |
| `deepseek-v4-pro` | 空闲 | 0.15 | 4.50 | 13.50 |

高峰时段默认是**北京时间工作日 9:00–12:00、14:00–18:00**；周末和配置里的节假日按空闲价。
价格来源：[DeepSeek 官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
官方调价后，在插件配置的「价格表」里改一下即可。

---

## 安装

### 方式一：AstrBot 插件市场

在 AstrBot WebUI 的「插件」页搜索 `astrbot_plugin_api_budget` 安装（如果已上架）。

### 方式二：手动安装

1. 把整个 `astrbot_plugin_api_budget` 文件夹放到 AstrBot 的
   `data/plugins/` 目录下；
2. 在 WebUI「插件」页点击重载（或重启 AstrBot）；
3. 启用插件，并按需修改配置；
4. 确认 AstrBot 的 `admins_id` 里包含你的 QQ 号，否则 `/额度` 指令不会响应。

Windows 下的路径通常类似：

```text
E:\AsrBot\instances\<实例ID>\core\data\plugins\astrbot_plugin_api_budget\
```

---

## 指令

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/额度` | 今日总花费、各 Key 花费/预算、token 明细 | 管理员 |
| `/额度明细` | 今日按模型拆分（高峰/空闲分别计价） | 管理员 |
| `/额度历史` | 最近 7 天每日花费 | 管理员 |
| `/额度历史 14` | 最近 14 天（1–30） | 管理员 |

别名：`/花费`、`/apikey`、`/用量`、`/额度查询` 等同 `/额度`；`/花费明细`、
`/花费历史` 同理。

> QQ 官方机器人只能被动回复，指令需要 @机器人 或按平台要求触发。

---

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `daily_budget` | `2.0` | 每个 Key 每日预算（人民币元），`0` = 只统计不拦截 |
| `budget_overrides` | `{}` | 给特定 Key 单独设预算的 JSON，见下 |
| `enforce_chat` | `true` | 聊天 Key 超限时拦截 |
| `enforce_websearch` | `true` | 联网搜索 Key 超限时移除搜索工具 |
| `block_message` | 见默认 | 超限提示，可用 `{key}`、`{budget}`、`{spent}`、`{remaining}` 等占位符 |
| `pricing_json` | DeepSeek 默认价 | 价格表 JSON，可加任意模型 |
| `peak_windows` | `["09:00-12:00","14:00-18:00"]` | 高峰时段 |
| `peak_weekdays` | `[1,2,3,4,5]` | 高峰星期（1=周一） |
| `holidays` | `[]` | 按空闲价计费的日期，如 `["2026-10-01"]` |
| `websearch_price_per_1000` | `0.0` | 搜索价格（元/1000 次成功请求） |
| `websearch_tool_names` | `[]` | 额外要统计的搜索工具名（内置已包含 AstrBot 自带工具） |
| `count_failed_searches` | `false` | 失败的搜索调用是否计数 |
| `reset_hour` | `0` | 每天几点重置额度（0–23） |
| `history_days` | `7` | `/额度历史` 默认天数 |
| `timezone` | `Asia/Shanghai` | 时区 |
| `db_path` | 空 | 数据库路径，留空自动查找 `data/data_v4.db` |

### 单独设置某个 Key 的预算

`budget_overrides` 填 JSON。键可以是 provider source id，也可以是 `websearch:<provider>`：

```json
{
  "deepseek": 5,
  "mimo_tts": 3,
  "websearch:deepseek": 1
}
```

### 自定义价格表

`pricing_json` 的格式（单位：人民币元 / 百万 token）：

```json
{
  "deepseek-flash": {
    "peak": { "cached_input": 0.04, "input": 2.0, "output": 8.0 },
    "off_peak": { "cached_input": 0.02, "input": 1.0, "output": 4.0 }
  },
  "my-model": {
    "peak": { "cached_input": 0, "input": 3.0, "output": 6.0 },
    "off_peak": { "cached_input": 0, "input": 3.0, "output": 6.0 }
  },
  "default": {
    "peak": { "cached_input": 0.04, "input": 2.0, "output": 8.0 },
    "off_peak": { "cached_input": 0.02, "input": 1.0, "output": 4.0 }
  }
}
```

- 模型名匹配不区分大小写，支持带日期后缀（如 `deepseek-flash-1017` 会匹配
  `deepseek-flash`）；
- 没匹配到的模型使用 `default`，并在输出里提示「按默认价估算」；
- 只想设一个统一价，可以不写 `peak` / `off_peak`，直接写
  `{"input": 2, "output": 8}`。

---

## 已知限制

- **语音 TTS 费用暂不统计**：MiMo 等 TTS 接口不返回 token 用量，插件无法计算；
  如果你用语音回复，实际花费会比显示的高。
- **联网搜索价格需要自己填**：默认 `0`（DeepSeek Search 在 2026-11-01 之前免费）。
  2026-11-01 起官方按 5 元/1000 次收费，届时把 `websearch_price_per_1000` 改成 `5`。
- **只统计本地 Agent**：依赖 AstrBot 的 `provider_stats`，第三方 Agent Runner
  （Dify / Coze 等）或某些插件自己发起的调用不会入库，也就不会计入。
- **多个搜索 Key 轮换**：按 provider 汇总，不细分到具体某一个 Key。
- **并发极高时可能小幅超支**：拦截检查发生在请求发出前，统计的是已经完成的请求；
  同一瞬间多个请求可能各自通过检查。个人/小群场景可以忽略。
- **数据库结构依赖 AstrBot 版本**：本插件在 AstrBot 4.29 上开发验证；如果未来
  `provider_stats` 表结构变化，可能需要更新插件。

---

## 隐私与安全

- 插件只以只读方式打开 `data/data_v4.db`，不会修改 AstrBot 数据；
- 不保存、不打印、不上传任何 API Key；聊天输出里只显示 `sk-***后四位`；
- 插件自己的状态（搜索计数、提示冷却）保存在
  `data/plugin_data/astrbot_plugin_api_budget/state.json`，可随时删除重置。

---

## 开发与测试

```bash
# 单元测试（不需要 AstrBot）
python -m unittest discover -s tests -v

# 可选：用 AstrBot 自带的 Python 做加载 + 拦截冒烟测试
# 两个脚本会自己建临时数据库，不会读取或修改机器人真实数据
export PYTHONPATH=/path/to/AstrBot            # Windows: set PYTHONPATH=E:\...\core
<AstrBot实例>/venv/Scripts/python.exe tests/smoke_plugin.py
<AstrBot实例>/venv/Scripts/python.exe tests/smoke_guard.py
```

代码风格：`ruff format .` / `ruff check .`。

---

## 发布到 GitHub

1. 修改 `metadata.yaml` 里的 `author`、`repo` 为你自己的信息；
2. 在 GitHub 新建仓库 `astrbot_plugin_api_budget`；
3. 执行：

```bash
git init
git add .
git commit -m "feat: AstrBot API daily budget plugin"
git branch -M main
git remote add origin https://github.com/<你的用户名>/astrbot_plugin_api_budget.git
git push -u origin main
```

4. 想上架 AstrBot 插件市场的话，按
   [插件发布文档](https://docs.astrbot.app/dev/star/plugin-publish.html)
   提交仓库即可。

---

## License

[MIT](LICENSE)
