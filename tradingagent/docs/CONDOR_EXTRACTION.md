# Condor dependency inventory

Reference source: `D:/develop/tradingagent/condor-main` (read only).
Inventory date: 2026-10-04. This increment copies no Condor source code.

| Source location | Coupling found in source | Decision |
| --- | --- | --- |
| `config_manager.py:532–622` | `get_client` imports `HummingbotAPIClient`; pooled clients bind server configuration and credentials | Build independent configuration/composition; do not copy client pooling into the kernel |
| `condor/fetchers/orders.py:20` | Calls `client.trading.get_active_orders`; returns upstream `data` dictionaries | Replace with owned ObservedOrder objects behind read-only AccountPort; real execution is outside v1 |
| `condor/fetchers/market_data.py` | Calls `client.market_data.get_prices/get_candles/get_historical_candles` | Provider-specific normalization belongs in the Binance adapter |
| `condor/fetchers/portfolio.py` | Normalizes connector/account dictionaries, float valuations and exchange-specific account deduplication | Use Decimal Balance/Position/AccountSnapshot; no raw connector payload in core |
| `condor/fetchers/models.py` | Models include Hummingbot Controller/Bot/Executor concepts and float PnL | Do not reuse those models as domain models |
| `condor/server_data_service.py:347–350` | `_get_client` obtains the Hummingbot client through ConfigManager; caches server-scoped portfolio/bot/executor data | Reference bounded caching and subscription design; do not copy SDS |
| `mcp_servers/hummingbot_api/hummingbot_client.py:12` | Direct Hummingbot SDK dependency; credentials, initialization and retries | Entirely excluded from core dependencies |
| `mcp_servers/hummingbot_api/server.py` | Tool registration dispatches through the Hummingbot client and profiles | Future MCP transport and schema handling belong exclusively in adapters |

Design references for later tasks:

- `condor/runtime/sessions.py`: session lifecycle, serialized prompts, timeout and cancellation.
- `condor/agents/engine.py`: gather → decide → persist; short decision sessions.
- `condor/agents/risk.py`: deterministic risk gating and shutdown restrictions.
- `condor/runtime/confirmations.py`: confirmation as a separate execution gate.
- `condor/runtime/conversations.py`: structured decision history.

These are references, not imported dependencies. Every migrated capability will
be implemented behind an owned contract and tested before any source reuse.
Any later source copying must preserve the source license and attribution.

Deleting any real provider must leave Fake/Replay usable. High-frequency
sampling and mechanical risk stay deterministic; a provider never owns Session,
Memory, Loop, business state or execution authority.

## 当前实现来源复核（2026-10-05）

T01–T16 当前离线实现的分类为：**设计参考后独立实现，没有发现直接复制或导入的 Condor 模块。**
上文是最初依赖盘点；本节补充整个当前产品目录的实际源码审计，不将“没有导入”单独当作“没有复制”的证据。

| 检查 | 范围 | 实际结果 |
| --- | --- | --- |
| 非空完整文件 SHA256 | 当前 145 Python + 17 前端文件；参考 367 Python + 5 前端文件 | 完全相同项 0 |
| 去除文档字符串的函数 AST | 保留标识符；至少 8 行、50 节点；当前 363 / 参考 3021 函数 | 相同项 0 |
| Condor/Hummingbot 导入 | 当前产品全目录；静态及字面量动态导入 | 0 |
| Python 源码解析 | 上述 Python 文件 | 两侧错误 0 |
| 发行与依赖 | pyproject.toml，只打包 agent_platform | 没有 Condor/Hummingbot 依赖 |

排除测试、虚拟环境、依赖目录、构建产物和 output；当前目录包含所有产品 Python、JS、CSS、HTML。
该比对不检测任意改名/改写片段、短函数或语义克隆，也不能单独证明作者来源。
结论结合依赖、实际实现及此前抽离记录，不宣称进行了完整代码溯源证明。

本次实际产品快照 SHA256：`8283e6f1ac6cf597ba0d16eaaf2bca5c90e4b3593ba69493f91828f98da269ff`。
实际参考快照 SHA256：`5662a808a1070a103691e96526e8e332e18f6bc0052f9820e5875737d3e863d7`。
快照算法与每个文件的 SHA256 已写入审计脚本/结果，修改源码后需重新审计，不能沿用本次数字。

- [完整审计结果](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/output/verification/condor-reuse-audit-20261005.json)
- [可复查的只读脚本](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/output/verification/condor-reuse-audit-20261005.py)
- [中文项目结构、对应表与运行链路](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/docs/PROJECT_OVERVIEW.md)

Condor 本地 LICENSE 标注 MIT，版权行为 `Copyright (c) 2023 Hummingbot Foundation`。
本次没有发现直接复制的产品模块；后续若复制代码，应逐文件保留来源与许可证说明，不用“独立实现”掩盖实际复制。

当前具体对应：会话业务/风格由自有 SessionService 与 SQLite 实现，决策由 DecisionRuntime/DecisionService 实现，
风险由 Decimal 现货 RiskService 实现，历史由 SQLite 类型化事件/冻结复盘实现，运行生命周期由 RuntimeSupervisor 实现。
Condor 的聊天 ACP 会话、Markdown journal、learnings、Hummingbot SDK、Telegram 与 Bot/Executor 模型均未迁移。
Agent OS 目前只实现可选 MCP 清单探测；它不是当前已经接通的编排框架。
