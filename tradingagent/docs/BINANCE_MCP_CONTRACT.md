# Binance Agent OS MCP接入边界

核对：2026-10-05。T10是可选适配器，Direct继续作为首版主链路。

2026-10-07重新核对官方页面：Market data（ticker/orderbook/candles/funding）为公共无认证范围；Account、Trade、Transfer另行授权。当前Paper只需公共行情与虚拟钱包，不要求MCP账户登录。Codex客户端的连接不自动提供本Python服务授权；不复制桌面Token。实际Direct REST/WS已连接，当前无有效快照源自本机未同步时钟触发严格未来数据守卫，不是缺MCP。插件目录本次查询因网络失败，未验证可安装项或当前连接状态，不声称已连接。

## 已核对与未验证

[Binance官方文档](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic)
列出远程MCP入口、市场/Account/Trade/Transfer范围，以及Agentic子账户和可选主账户只读视图。
官方示例使用HTTP传输。公开页面描述授权过期后断开并重新连接；没有为本项目验证自动续期和常驻客户端。
本文不据此推定主账户Spot余额可用、区域资格满足、工具名/Schema固定或支持testnet。

具体工具名、输入/输出Schema、分页、权限错误、主账户scope、授权撤销和续期均缺本机实测证据。
桌面客户端的已有登录不是本程序授权；本项目不读取或复制桌面Token。
不安装SDK、不新建Agentic子账户、不授权Trade/Transfer、不试单或转入资金。

## 离线可推进的契约

只探测有界tools/list元数据，使用注入Fake传输测试，未提供传输时立即disabled。
工具描述与annotations仅是非可信元数据；readOnlyHint不能作为资金安全或账户scope证明。
报告仅保存工具名和规范化Schema哈希，不输出描述、Schema默认值、凭据或服务器异常原文。
Schema增减或输入/输出哈希变化必须重新验证；不自动接纳新增工具。

真实工具调用与MarketDataPort/AccountPort映射必须等以下证据同时具备：

- 本程序独立授权和只读scope，续期/撤销行为已验证。
- 固定endpoint、协议版本和有界传输，禁止重定向到任意端点。
- 从该服务器tools/list取得的确切名称与Schema哈希，人工审核读语义，不能靠名称/注解猜测。
- 主账户或明确目标账户/wallet身份确认，BTCUSDT Spot数据映射与失败行为录制。

在这些证据缺失时不实现虚构的Binance工具映射；能力不可用不能返回空余额/零持仓。
Application/Domain保持不依赖MCP类型；策略和模型不能持有通用call_tool。
真实MCP保持未启用，不阻塞T11及其他离线任务；安装SDK与真实授权留给用户管理。

## 协议依据

[MCP工具规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
规定tools/list分页、inputSchema及可选outputSchema；工具注解不应被当成可信权限。
[传输规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
说明Streamable HTTP可包含JSON/SSE响应，不把Web页面SSE与MCP工具流混为一谈。
[授权规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
描述HTTP授权流程；具体Binance实现及Token生命周期仍需本程序单独验证。
