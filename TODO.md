# 资金费率套利机器人 — 开发路线图

> 详细设计见 `docs/plan.md`
> 最后更新：2026-04-16

---

## 已完成

### Phase 0 — 修复现有问题
- [x] **0.1** 修复 `close_pnl` 始终为 0 — 开平仓价差现在正确计入 P&L
- [x] **0.2** 启用 `trade_logs` 写入 — 每次开/平仓各写 2 条（spot + futures 腿）
- [x] **0.3** 接通通知方法 — `on_open` / `on_close` / `on_funding` / `on_error` 全部接入
- [x] **0.4** 引入 `BaseExecutor` 抽象基类 — `BinanceExecutor` 与 `SimulatedExecutor` 统一接口

### Phase 1 (v5) — 上实盘前必须完成
- [x] **1.1** BNB 手续费抵扣 — `use_bnb_discount` 配置项，BNB 不足时 Telegram 告警
- [x] **1.2** 开仓顺序：合约先行 — `order_priority: futures_first`，现货失败自动回滚合约
- [x] **1.3** 分批下单 — `_split_order()` 按币种阈值拆单，批间隔 500ms
- [x] **1.4** 基差感知开平仓 — `fetch_basis()` 检查入场基差，`open_basis` / `close_basis` 写入 DB

### Phase 1.5 (v5.1) — 安全底线补全（上实盘前）
- [x] **1.5** 部分成交检测 — `executor._check_fill()` 校验 `executedQty`，不足 99% 触发 rollback
- [x] **1.6** 滑点超标硬中止 — 滑点超 `max_slippage` 时立即回滚两腿，不再仅 warning
- [x] **1.7** 过滤负费率方向 — `screener.screen()` 只取 `rate > 0`，现货账户无法裸空
- [x] **1.8** `min_profitable_rate` 上调 — 0.00005 → 0.0005；新增 `min_holding_hours: 24` 保护期，防震荡开平手续费失血
- [x] **1.9** 账实对账器 — 新建 `reconciler.py`，启动 + 每小时核查合约持仓 vs DB，偏差 >5% 或孤儿仓 → TG 告警 + 暂停开仓
- [x] **1.10** 日亏损上限接线 — `task_scan_and_open` 读 `daily_pnl`（含平仓 PnL），超阈值当日停开
- [x] **1.11** 结算窗口封锁 — HH:59:30–HH:00:30 UTC 禁止开平仓，防结算时间竞争
- [x] **1.12** 预期回本门槛 — 开仓前验证 `min_holding_hours` 内累计资金费 ≥ 双程手续费 × 1.5
- [x] **1.13** APScheduler 防重入 — 所有 Job 加 `max_instances=1, coalesce=True`

---

## 待完成

### Phase 2 (v6) — 实盘第一周补全（安全网）

- [ ] **2.0** Rollback 失败 TG 告警 — `executor._rollback_spot/_rollback_futures` 中 `logger.critical` 改为同步推送 `notifier.on_error`
- [ ] **2.0** WebSocket markPrice 实时监控 — 订阅 `!markPrice@arr`，持仓期间费率符号翻转秒级触发平仓评估（当前 REST 轮询最坏延迟 10min）
- [ ] **2.0** 累计 funding 偏差监控 — 每小时对比实际累计 funding vs 开仓时预期，偏差 >50% 主动平仓
- [ ] **2.0** 清理死代码 — `rate_reverse_count` 列 + `increment_reverse_count`/`reset_reverse_count` 方法未被任何逻辑调用，要么实现要么删除

### Phase 2 (v6) — 实盘稳定后，策略优化

- [ ] **2.1** 费率预测入场优化
  - `screener.py` 评分公式加入 `nextFundingRate` 权重（10%）
  - 预测费率上升 → 加分；方向即将反转 → 减分；缺失 → 中性

- [ ] **2.2** 费率结算前智能检查
  - 新增 `task_pre_settlement_check()`
  - APScheduler cron：UTC 23:55 / 7:55 / 15:55（结算前 5 分钟）
  - 持仓预测费率不利时提前平仓；无仓位但即将结算且费率极高时快速开仓
  - 依赖：2.1（需要预测费率数据）

- [ ] **2.3** 复利自动再投入
  - `capital.py` 新增 `recalculate_from_balance()`
  - 每月 1 号 UTC 0:00 读取账户余额，重新计算 `CapitalPlan`
  - 安全限制：单次调整幅度 ≤ ±20%，余额低于 `min_capital` 时拒绝并通知

- [ ] **2.4** 报表增强
  - `position.py` 新增查询：按 Tier 分组、按持仓时长、胜率、最大回撤
  - `results.py` 新增报表段：Tier 对比、手续费趋势、基差损益、最大单次亏损

### Phase 3 (v7) — 持续迭代

- [ ] **3.1** 动态参数自适应（新建 `adaptive.py`）
  - 基于近 7 天均费率动态调整开仓门槛（默认关闭，`adaptive.enabled: false`）
  - 牛市（均费率 > 0.03%）→ 门槛降 20%；低迷期（< 0.01%）→ 门槛升 30%
  - 所有调整有硬约束上下限
  - 需积累足够交易历史后才有意义

- [ ] **3.2** Maker 单优化（默认关闭）
  - 限价单挂对手最优价，3 秒未成交撤单改市价
  - 合约端费率：Taker 0.04% → Maker 0.02%
  - 风险：两腿时间差增大，实施放最后，默认 `order_type: market`

### 独立任务 — 美股费率监控

- [ ] **S.1** `stock_monitor.py` 独立脚本（不影响主 bot）
  - 数据源：Kraken xStocks、Hyperliquid、Ostium
  - 标的：AAPL / TSLA / NVDA / MSFT / AMZN / META / GOOG / SPX / NDX
  - 每 5 分钟采集，存 `stock_rates.db`，每日 Telegram 日报
  - 目标：积累 3~6 个月数据，评估美股套利可行性
  - 实盘切入前置条件见 `docs/plan.md` 第四部分

---

## 暂不实施

| 项目 | 原因 |
|------|------|
| 跨所费率差套利（P2-9）| 双所资金管理 + 转账 + 回滚复杂度极高，收益不确定 |
| 美股多资产架构改造 | 在监控数据证明可行性前属于过度工程，待 S.1 积累数据后再评估 |
| 预言机风险监控 | 依赖具体 DeFi 平台选型，当前无法落地 |
