# JEV 多会话工作台 Implementation Plan

> 执行方式：superpowers:executing-plans，由当前代理原地实施。用户自主推进和减少测试授权优先，不新增确认关卡、子代理、提交或工作树。

**Goal:** 单一向导创建多个独立后台 JEV 建议或自动模拟会话。
**Architecture:** 根任务索引 + 每任务独立 SQLite/runtime，共用原模型费用库；保留根库旧会话。建议复用决策链但不进入执行器。
**Tech Stack:** 现有 Conda Python / FastAPI / SQLite / 原生 JavaScript。
**Spec:** docs/JEV_WORKBENCH_SPEC.md

## Global Constraints

不安装依赖；不重置资金/费用；不真实下单。0–100 整数风格明确确认并保存版本。每笔成交继续严格存档。常规 Agent 暂缓。

## Review Focus

创建超时重试复用相同钱包；显示切换不改变执行目标；建议不能成交；重启禁止自动收费；旧关闭会话档案不能误归当前币种。

### Task 1: 建议模式与任务生命周期

Files: application/futures_trading.py、domain/trading_runtime.py、application/jev_tasks.py、adapters/sqlite/jev_tasks.py、domain/jev_tasks.py、config.py；tests/integration/test_jev_tasks.py。
Interfaces: JevTaskCreate；JevTaskWorkbench.open/create/list/view/start/pause，pause(..., close=True)结束会话。
- [x] 测试先行：建议非 WAIT 仍 command_id=None / 钱包数量零；两个任务独立运行；同 ID 重试相同 session_id；预算路径完全一致；恢复 paused。
- [x] 运行新测试确认失败。
- [x] 实现固定任务用途、索引及独立服务上下文管理；导入根库会话只读身份，版本化 start/pause。
- [x] 运行新测试，检查无收费、原资金未写。

### Task 2: 统一 Web 与向导

Files: web/jev_task_routes.py、web/app.py、web/templates/jev-workspace.html、web/static/jev-workspace.js/css、tools/start-jev-paper-live.py。
Interfaces: /api/jev-tasks、/{task_id}、start/pause/close/history/archive，/workbench。
- [x] 测试CSRF、版本冲突、归属隔离；严格输入沿现有Pydantic校验，未另增镜像测试。
- [x] 实现三步向导与左侧会话栏、工作区恢复暂停、历史行情、决策日志与档案导出。
- [x] 运行精简离线 API/JS/静态检查；浏览器查看实际渲染。

### Task 3: 保留与接续

Files: DEVELOPMENT_STATUS.md、IMPLEMENTATION_LEDGER.md、PRODUCT_SPEC.md。
- [x] 记录现有 session_not_running 前端修复及证据。
- [x] 核对原 8776 身份后仅重载它，原会话、钱包、40条费用和用户 README 保留。
- [x] 验证真实页面与后台状态；标注真实并行收费与长稳未验证。记录常规 Agent 暂缓方案。

完成证据：docs/DEVELOPMENT_STATUS.md 最新段。4项新增测试通过，原27项核心回归此前通过；离线浏览器双会话全流程通过；真实原钱包保留/无新增收费。真实多会话长稳及自主成交不在本向导开发的已验收范围。
