# Futures Public Runtime Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline，逐任务TDD。用户自主授权与指定目录优先：不新工作树、不提交、不安装；依据既有ledger保存裁定与验证。

**Goal:** 实现可独立读取USDT合约价格/盘口、交易规则及已结算资金费的公共Provider。
**Architecture:** 新领域DTO/Port；独立Provider组合既有公共Client，扩展固定公共GET白名单，核心不接触wire格式；历史客户端独立继续工作。
**Tech Stack:** 正式Conda Python3.12、既有Pydantic/Decimal/httpx/pytest；无依赖变更。
**Spec:** [FUTURES_MARKET_SPEC.md](FUTURES_MARKET_SPEC.md)。

## Global Constraints

- 仅USDT永续公共数据，明确symbol，无账户/模型Key或真实订单。
- mark/book独立5秒、snapshot请求总限5秒；资金费(after,through]毫秒对齐、最多31天/1000每页/4页。
- 固定GET白名单、4MiB与重复JSON守卫、429/418冷却、无redirect/retry。
- 公开filters与固定模拟维持规则分离；规则缓存300秒/32个币种，过期错误不回退。
- 原Spot、用户80/v1暂停会话、到期模型预算保持事实。

## Review Focus

- 稳定币/中文符号及极小tick：wire必须保留字符串Decimal，不硬写币种名单。
- 两HTTP请求之间时钟推进或book停滞：接收时间不能使旧价格变新鲜。
- 市价和普通LOT过滤器不同：分别保留，不按precision推步长。
- funding恰在窗口边界、满页、Special/缺类型：只完整Regular批次，不能漏事件后宣称窗口完整。
- 限流/取消/异常形状及规则过期：不能自动重试或偷偷用旧数据。

## FM1 运行事实和安全报价

**Files:** domain/futures_market.py、ports/futures_market.py、adapters/binance_direct/futures_market.py；修改futures_public.py白名单；tests/adapters/test_futures_market.py。
**Produces:** snapshot(symbol)->FuturesMarketSnapshot(quote,index_price,displayed_funding_rate,next_funding_at,bid_quantity,ask_quantity)。
- [x] 先写真实MockHTTP测试：ETH mark2000/bid1999/ask2001、中文符号正确、仅公共GET；未来/过期/交叉/缺字段/非字符串价格拒绝。实际缺模块RED。
- [x] 实现DTO/Port/Provider和仅symbol的premiumIndex/bookTicker白名单，目标GREEN；HTTP取消/冷却/超时/超限沿用真实client。
- [x] 原session_market/Paper/架构回归；记录任务证据，不把provider等同运行后台。

## FM2 合约交易规则和结算分页

**Files:** 继续FM1文件与tests/adapters/test_futures_market.py。
**Produces:** rules(symbol)->FuturesContractRules；settlements(symbol,after,through)->FuturesFundingWindow。
- [x] rules测试tick0.01/LOTstep0.001/MARKETstep0.01（precision故意冲突），唯一合约/全部必需filters/错资产/重复/零字段/缓存刷新失败。实际方法缺失RED。
- [x] 读取并保留PRICE/LOT/MARKET_LOT/MIN_NOTIONAL/PERCENT_PRICE/marketTakeBound；不猜维持档位，GREEN。
- [x] funding实际方法缺失RED；普通结算完整输出、空批次、1000+1分页/start+1ms、窗口上限、非对齐/未来/Special/缺mark/重复/乱序/错币种/满4页未覆盖拒绝，GREEN。
- [x] 原目录历史、领域、Paper、架构回归与Ruff。

## FM3 公共数据验收和接续

**Files:** FUTURES_MARKET_VERIFICATION.md、DEVELOPMENT_STATUS、IMPLEMENTATION_LEDGER、总计划/产品规格。
- [x] 独立只读代码复核；重要问题真实RED→GREEN，当前全套与源码格式通过。
- [x] 有界只读真实ETH及另一个合约报价/规则/资金费探测，保存公开证据；网络失败也保存，不制造模拟走势图。
- [x] 更新任务完成和未装配运行/JEV/Web边界、真实付费/Testnet仍缺口；不构建新wheel或重启/恢复用户操盘。

FM1–FM3数据层范围已完成：83项新增专项，连领域/持久/历史/架构172项/115subtests；最终全套1372 passed / 150 subtests（135.38秒），Ruff与299文件format通过。独立复核两项P2已实际RED→GREEN，真实ETH/SOL公共数据通过。证据 [FUTURES_MARKET_VERIFICATION.md](FUTURES_MARKET_VERIFICATION.md)，后续清单 [REMAINING_WORK.md](REMAINING_WORK.md)。

预检：FM1报价消费现有FuturesPaperQuote；FM2结算消费现有FuturesPaperFunding；两者独立版本源和时间已匹配。新Port只依赖Domain。串行任务本轮inline实施，单次最终独立review使用新上下文；既有自主授权无需按技能默认重复停下来审批。
