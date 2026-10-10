# Conda 开发环境

开发根目录：`D:\develop\tradingagent\Blockchain-Trading-Lab\tradingagent`。
环境与依赖由用户安装；Agent 使用已准备好的项目环境进行开发与验证。

## 当前环境

2026-10-05 已验证 tradingagent 环境：Python 3.12.14、Pydantic 2.13.5、
FastAPI 0.142.2、pytest 9.1.1、Ruff 0.16.10；pip check 无冲突。

```powershell
conda activate tradingagent
Set-Location -LiteralPath 'D:\develop\tradingagent\Blockchain-Trading-Lab\tradingagent'
python -m pytest -q
python -m ruff check .
```

Agent 可直接使用此环境的 python.exe，无需改变用户终端的激活状态。

## 新机器由用户安装

```powershell
conda env create -f environment.yml
conda activate tradingagent
python -m pip check
```

若已有同名环境，先核对用途，不删除或覆盖其他项目环境。
依赖范围见 environment.yml；升级后重新验证测试和依赖兼容性。
可选的本地包安装由用户执行 python -m pip install -e .。

## 依赖用途

| 依赖 | 用途 |
| --- | --- |
| Python 3.12 / pip | asyncio、Decimal、sqlite3、环境内的包管理 |
| pydantic / pydantic-settings | 领域校验与严格配置 |
| httpx / websockets | 只读 REST 与公共行情 |
| fastapi / uvicorn / jinja2 | 本地 Web 与页面模板 |
| PyYAML / keyring / tzdata | 非敏感配置、本机凭据、时区数据 |
| pytest / pytest-asyncio / ruff | 行为测试、异步测试与静态检查 |

MCP 阶段再由用户安装当时确认版本的官方 Python SDK。
LLM 供应商尚未确定，收费请求默认关闭。

## 后续联调配置

- Binance 只读 API Key/Secret 保存在本机，关闭交易、提现和划转权限。
- 实际账户范围及 BTCUSDT 历史成交读取权限需验证。
- LLM 供应商、模型、密钥和日预算未配置时使用规则或 Fake。
- 用户纪律和数量上限未配置时，不输出具体仓位数量。

密钥不发送到聊天，不写入 Git、日志或业务数据库。

参考：[Conda 环境管理](https://docs.conda.io/projects/conda/en/latest/user-guide/tasks/manage-environments.html)。
