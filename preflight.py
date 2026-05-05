"""
预检脚本 — 逐项检查运行环境是否就绪

使用方式:
  python preflight.py              # 检查全部
  python preflight.py --monitor    # 只检查监控模式需要的项目
  python preflight.py --live       # 实盘：额外硬卡 spot/futures USDT 余额 ≥ capital
  python preflight.py --live --capital 410   # 用 CLI 值覆盖 config.yaml 的 initial_capital
"""
import sys
import os
import argparse

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"
WARN = "⚠️"


def check(name: str, ok: bool, msg_pass: str = "", msg_fail: str = "") -> bool:
    if ok:
        print(f"  {PASS} {name}" + (f" — {msg_pass}" if msg_pass else ""))
    else:
        print(f"  {FAIL} {name}" + (f" — {msg_fail}" if msg_fail else ""))
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", action="store_true", help="只检查监控模式")
    parser.add_argument("--live", action="store_true",
                        help="实盘模式：额外硬卡 spot/futures USDT 余额 ≥ capital")
    parser.add_argument("--capital", type=float, default=None,
                        help="覆盖 config.yaml 的 initial_capital（用于实盘余额校验）")
    args = parser.parse_args()
    monitor_only = args.monitor
    live_mode = args.live and not args.monitor

    all_ok = True

    # ------------------------------------------------------------------
    print("\n📦 1. Python 环境")
    # ------------------------------------------------------------------
    v = sys.version_info
    check("Python 版本", v >= (3, 10), f"{v.major}.{v.minor}.{v.micro}", "需要 Python 3.10+")

    # 检查依赖包
    deps_monitor = ["ccxt", "yaml", "aiohttp", "loguru"]
    deps_trade = ["binance.spot", "binance.um_futures", "apscheduler"]

    for pkg in deps_monitor:
        try:
            __import__(pkg.split(".")[0] if "." not in pkg else pkg.rsplit(".", 1)[0])
            check(f"  {pkg}", True)
        except ImportError:
            all_ok = False
            check(f"  {pkg}", False, msg_fail="pip install -r requirements.txt")

    if not monitor_only:
        for pkg in deps_trade:
            try:
                mod = pkg.replace(".", "/")
                __import__(pkg.split(".")[0])
                check(f"  {pkg}", True)
            except ImportError:
                all_ok = False
                check(f"  {pkg}", False, msg_fail="pip install -r requirements.txt")
    else:
        print(f"  {SKIP} 交易依赖 — 监控模式不需要")

    # ------------------------------------------------------------------
    print("\n🔑 2. 配置文件")
    # ------------------------------------------------------------------
    config_exists = os.path.exists("config.yaml")
    check("config.yaml 存在", config_exists, msg_fail="请从 config.yaml 模板创建")

    if config_exists:
        import yaml
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)

        # API Key
        api_key = cfg.get("exchanges", {}).get("binance", {}).get("api_key", "")
        has_key = api_key and "YOUR_" not in api_key
        check("Binance API Key", has_key,
              f"{api_key[:8]}...{api_key[-4:]}" if has_key else "",
              "请填入你的 API Key")
        if not has_key:
            all_ok = False

        api_secret = cfg.get("exchanges", {}).get("binance", {}).get("api_secret", "")
        has_secret = api_secret and "YOUR_" not in api_secret
        check("Binance API Secret", has_secret,
              "已配置" if has_secret else "",
              "请填入你的 API Secret")
        if not has_secret:
            all_ok = False

        # Telegram（可选）
        tg_token = cfg.get("telegram", {}).get("bot_token", "")
        tg_ok = tg_token and "YOUR_" not in tg_token
        if tg_ok:
            check("Telegram Bot Token", True, f"{tg_token[:10]}...")
        else:
            print(f"  {WARN} Telegram Bot Token — 未配置（可选，不影响运行）")

        tg_chat = cfg.get("telegram", {}).get("chat_id", "")
        tg_chat_ok = tg_chat and "YOUR_" not in tg_chat
        if tg_chat_ok:
            check("Telegram Chat ID", True, tg_chat)
        else:
            print(f"  {WARN} Telegram Chat ID — 未配置（可选）")

        # 资金
        capital = cfg.get("initial_capital", 0)
        check("初始资金", capital > 0, f"${capital:,.0f}",
              "initial_capital 需要 > 0")

        # 实盘余额校验用：CLI --capital 优先，其次 config.initial_capital
        effective_capital = args.capital if args.capital is not None else capital

    # ------------------------------------------------------------------
    print("\n🌐 3. 网络连通性")
    # ------------------------------------------------------------------
    import asyncio

    async def test_binance():
        import ccxt.async_support as ccxt
        try:
            ex = ccxt.binance({"options": {"defaultType": "swap"}})
            await ex.load_markets()
            count = len([m for m in ex.markets.values() if m.get("swap")])
            await ex.close()
            return True, f"已连接，{count} 个永续合约"
        except Exception as e:
            return False, str(e)

    ok, msg = asyncio.run(test_binance())
    check("Binance API 连通", ok, msg, msg)
    if not ok:
        all_ok = False

    # ------------------------------------------------------------------
    if not monitor_only:
        print("\n🔐 4. API 权限检查 (交易模式)")
        # ------------------------------------------------------------------
        if config_exists and has_key and has_secret:
            async def test_permissions():
                from binance.spot import Spot
                from binance.um_futures import UMFutures

                results = {}

                # 现货余额
                try:
                    spot = Spot(api_key=api_key, api_secret=api_secret)
                    info = spot.account()
                    usdt = next(
                        (b for b in info["balances"] if b["asset"] == "USDT"),
                        {"free": "0"}
                    )
                    bal = float(usdt['free'])
                    results["spot_read"] = (True, f"USDT 余额: ${bal:,.2f}", bal)
                except Exception as e:
                    results["spot_read"] = (False, str(e)[:80], None)

                # 合约余额
                try:
                    futures = UMFutures(key=api_key, secret=api_secret)
                    balances = futures.balance()
                    usdt = next(
                        (b for b in balances if b["asset"] == "USDT"),
                        {"availableBalance": "0"}
                    )
                    bal = float(usdt['availableBalance'])
                    results["futures_read"] = (True, f"USDT 余额: ${bal:,.2f}", bal)
                except Exception as e:
                    results["futures_read"] = (False, str(e)[:80], None)

                return results

            perms = asyncio.run(test_permissions())

            for name, (ok, msg, _bal) in perms.items():
                label = "现货账户读取" if "spot" in name else "合约账户读取"
                check(label, ok, msg, msg)
                if not ok:
                    all_ok = False

            # 实盘模式：硬卡两侧 USDT 余额 ≥ effective_capital
            if live_mode:
                spot_bal = perms.get("spot_read", (False, "", None))[2]
                fut_bal = perms.get("futures_read", (False, "", None))[2]

                if spot_bal is not None:
                    if not check(
                        f"现货 USDT free ≥ ${effective_capital:,.2f}",
                        spot_bal >= effective_capital,
                        f"${spot_bal:,.2f} ≥ ${effective_capital:,.2f}",
                        f"现货 free=${spot_bal:,.2f} 不足；请充值或调低 initial_capital "
                        f"（如有现货持仓占用，先平仓再试）",
                    ):
                        all_ok = False

                if fut_bal is not None:
                    if not check(
                        f"合约 USDT availableBalance ≥ ${effective_capital:,.2f}",
                        fut_bal >= effective_capital,
                        f"${fut_bal:,.2f} ≥ ${effective_capital:,.2f}",
                        f"合约 availableBalance=${fut_bal:,.2f} 不足；请划转保证金或调低 initial_capital",
                    ):
                        all_ok = False
        else:
            print(f"  {SKIP} 需要先配置 API Key")
    else:
        print(f"\n  {SKIP} 4. API 权限 — 监控模式不需要")

    # ------------------------------------------------------------------
    if not monitor_only:
        print("\n🔒 5. 安全检查 (API 权限 & 账户模式)")
        # ------------------------------------------------------------------
        if config_exists and has_key and has_secret:
            import hmac as _hmac
            import hashlib
            import urllib.request
            import urllib.parse
            import urllib.error
            import time as _time
            from binance.spot import Spot as _Spot

            # 5a. API Key 权限位
            try:
                _spot = _Spot(api_key=api_key, api_secret=api_secret)
                perms = _spot.api_key_permission()

                no_withdraw = not perms.get("enableWithdrawals", True)
                if not check("API 提现权限已关闭", no_withdraw,
                             "enableWithdrawals=False",
                             "enableWithdrawals=True — 请在 Binance API 管理页关闭提现权限"):
                    all_ok = False

                if perms.get("ipRestrict", False):
                    check("API IP 白名单", True, "已绑定固定 IP")
                else:
                    print(f"  {WARN} API IP 白名单 — 未绑定（建议绑定服务器固定 IP，降低 Key 泄露风险）")

            except Exception as e:
                print(f"  {WARN} API 权限查询失败: {str(e)[:80]}")

            # 5b. Portfolio Margin 模式检测（PM 模式下 /fapi/ trading 端点会被限制）
            try:
                ts = int(_time.time() * 1000)
                qs = f"timestamp={ts}"
                sig = _hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                url = f"https://api.binance.com/sapi/v1/portfolio/account?{qs}&signature={sig}"
                req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    is_pm = resp.status == 200
            except urllib.error.HTTPError:
                is_pm = False  # 4xx = Classic 模式 / 未开通 PM
            except Exception as e:
                is_pm = None
                print(f"  {WARN} 账户模式检测失败: {str(e)[:60]}（请手动确认非 PM 模式）")

            if is_pm is not None:
                if not check("账户模式 — Classic",
                             not is_pm,
                             "独立 Spot + USDⓈ-M Futures 钱包",
                             "Portfolio Margin 模式已开启！请在网页端切回 Classic 再启动 bot"):
                    all_ok = False
        else:
            print(f"  {SKIP} 需要先配置 API Key")
    else:
        print(f"\n  {SKIP} 5. 安全检查 — 监控模式不需要")

    # ------------------------------------------------------------------
    print("\n📁 6. 目录")
    # ------------------------------------------------------------------
    os.makedirs("logs", exist_ok=True)
    check("logs/ 目录", os.path.isdir("logs"))

    # ------------------------------------------------------------------
    # 总结
    # ------------------------------------------------------------------
    mode = "监控模式" if monitor_only else ("实盘模式" if live_mode else "完整交易模式")
    print(f"\n{'='*50}")
    if all_ok:
        print(f"  {PASS} 全部检查通过! 可以启动 ({mode})")
        if monitor_only:
            print(f"     python main.py --monitor")
            print(f"     python main.py --monitor --capital 10000")
        else:
            print(f"     python main.py --capital 10000")
    else:
        print(f"  {FAIL} 有未通过的检查项，请修复后重试")
    print(f"{'='*50}\n")

    # 实盘模式必须 fail-fast：让 deploy.sh 的 `set -e` 拦住后续步骤
    # 其它模式保留旧行为（exit 0），避免破坏 simulate/monitor 路径
    if live_mode and not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
