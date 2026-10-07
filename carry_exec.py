"""
carry_exec.py - Delta 中性 carry 执行器（现货多 + 永续空）
==============================================================
已验证可用的执行路径（2026-09-12 实测通过）：
  现货腿：ccxt binance sandbox  -> testnet.binance.vision
  合约腿：直接 REST + HMAC      -> testnet.binancefuture.com  (ccxt 的 futures sandbox 已弃用)

【精度修正】核心改进：
  旧版先算现货数量、再四舍五入永续数量 → 两条腿数量不匹配，留下净敞口。
  新版**先按合约精度确定永续数量**（并向下取整到 stepSize），
  再按【永续实际数量】去买等量现货 → 净敞口严格为 0。

用法：
  python carry_exec.py status                 查看持仓/敞口/资金费
  python carry_exec.py funding                对账【实际收到的资金费】(来自交易所流水)
  python carry_exec.py open BTCUSDT ETHUSDT   双腿建仓
  python carry_exec.py close                  全部平仓（先平永续，再卖现货）
  python carry_exec.py auto 5                 按最新资金费率自动选前5名建仓
"""

import os
import sys
import json
import time
import hmac
import hashlib

import pandas as pd

_V = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_V) and _V not in sys.path:
    sys.path.insert(0, _V)

CFGP = os.path.join("data", "crypto", "exchange.json")
STATE = os.path.join("data", "crypto", "carry_state.json")
FBASE = "https://testnet.binancefuture.com"


def load_cfg():
    return json.load(open(CFGP, encoding="utf-8"))


# ===========================================================================
# 合约腿（直接 REST）
# ===========================================================================
class FuturesLeg:
    def __init__(self, cfg):
        import requests
        self.rq = requests
        self.key = cfg.get("apiKeyFut", "")
        self.sec = cfg.get("secretFut", "")
        self.ok = bool(self.key and self.sec)

    def _signed(self, path, params=None, method="GET"):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 10000
        qs = "&".join(f"{k}={v}" for k, v in p.items())
        sig = hmac.new(self.sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{FBASE}{path}?{qs}&signature={sig}"
        h = {"X-MBX-APIKEY": self.key}
        return (self.rq.get(url, headers=h, timeout=20) if method == "GET"
                else self.rq.post(url, headers=h, timeout=20))

    def filters(self, symbol):
        """取该合约的数量精度 stepSize 与最小量。"""
        r = self.rq.get(f"{FBASE}/fapi/v1/exchangeInfo", timeout=20)
        if r.status_code != 200:
            return 0.001, 0.001
        for s in r.json().get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        return float(f["stepSize"]), float(f["minQty"])
        return 0.001, 0.001

    def round_step(self, qty, step):
        if step <= 0:
            return qty
        n = int(qty / step)
        return round(n * step, 10)

    def mark_price(self, symbol):
        r = self.rq.get(f"{FBASE}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=15)
        return float(r.json()["markPrice"]) if r.status_code == 200 else 0.0

    def set_leverage(self, symbol, lev=2):
        r = self._signed("/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, method="POST")
        return r.status_code == 200

    def short(self, symbol, qty):
        r = self._signed("/fapi/v1/order", {"symbol": symbol, "side": "SELL",
                                            "type": "MARKET", "quantity": qty}, method="POST")
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text[:200]}
        j["_http"] = r.status_code
        return j

    def close_short(self, symbol, qty):
        r = self._signed("/fapi/v1/order", {"symbol": symbol, "side": "BUY", "type": "MARKET",
                                            "quantity": qty, "reduceOnly": "true"}, method="POST")
        return r.status_code, r.text[:200]

    def positions(self):
        r = self._signed("/fapi/v2/positionRisk")
        if r.status_code != 200:
            return []
        return [p for p in r.json() if abs(float(p.get("positionAmt", 0))) > 1e-12]

    def account(self):
        r = self._signed("/fapi/v2/account")
        return r.json() if r.status_code == 200 else {}

    def funding_income(self, start_ms=None, limit=1000):
        """【真实】资金费流水（交易所口径）。"""
        p = {"incomeType": "FUNDING_FEE", "limit": limit}
        if start_ms:
            p["startTime"] = start_ms
        r = self._signed("/fapi/v1/income", p)
        if r.status_code != 200:
            return pd.DataFrame()
        d = pd.DataFrame(r.json())
        if d.empty:
            return d
        d["time"] = pd.to_datetime(d["time"], unit="ms")
        d["income"] = pd.to_numeric(d["income"], errors="coerce")
        return d.sort_values("time")

    def commission(self, start_ms=None, limit=1000):
        p = {"incomeType": "COMMISSION", "limit": limit}
        if start_ms:
            p["startTime"] = start_ms
        r = self._signed("/fapi/v1/income", p)
        if r.status_code != 200:
            return pd.DataFrame()
        d = pd.DataFrame(r.json())
        if d.empty:
            return d
        d["time"] = pd.to_datetime(d["time"], unit="ms")
        d["income"] = pd.to_numeric(d["income"], errors="coerce")
        return d.sort_values("time")


# ===========================================================================
# 现货腿
# ===========================================================================
class SpotLeg:
    def __init__(self, cfg):
        import ccxt
        self.ex = ccxt.binance({"apiKey": cfg.get("apiKey"), "secret": cfg.get("secret"),
                                "enableRateLimit": True, "timeout": 20000})
        self.ex.set_sandbox_mode(True)
        self.ex.load_markets()
        self.ccxt = ccxt

    def buy_qty(self, base, qty):
        mkt = f"{base}/USDT"
        q = float(self.ex.amount_to_precision(mkt, qty))
        o = self.ex.create_market_buy_order(mkt, q)
        return q, o

    def sell_qty(self, base, qty):
        mkt = f"{base}/USDT"
        q = float(self.ex.amount_to_precision(mkt, qty))
        o = self.ex.create_market_sell_order(mkt, q)
        return q, o

    def price(self, base):
        return float(self.ex.fetch_ticker(f"{base}/USDT")["last"])

    def free(self, asset):
        return float(self.ex.fetch_balance()["free"].get(asset, 0) or 0)


# ===========================================================================
# 协调器
# ===========================================================================
class Carry:
    def __init__(self):
        self.cfg = load_cfg()
        self.fut = FuturesLeg(self.cfg)
        self.spot = SpotLeg(self.cfg)
        self.state = self._load()

    def _load(self):
        if os.path.exists(STATE):
            try:
                return json.load(open(STATE, encoding="utf-8"))
            except Exception:
                pass
        return {"positions": {}, "trades": [], "opened_at": None}

    def _save(self):
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(self.state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------- 建仓（精度修正版）----------
    def open(self, symbols, notional_each=100.0, lev=2):
        print("=" * 74)
        print(f"Delta 中性建仓（每币 {notional_each:.0f} USDT，杠杆 {lev}x）")
        print("=" * 74)
        for sym in symbols:
            base = sym.replace("USDT", "")
            step, minq = self.fut.filters(sym)
            mp = self.fut.mark_price(sym) or self.spot.price(base)
            # ① 先按【合约精度】确定永续数量（向下取整到 stepSize）
            raw = notional_each / mp
            qty = self.fut.round_step(raw, step)
            if qty < minq:
                print(f"  {sym}: 数量 {qty} < 最小 {minq}，跳过"); continue
            print(f"\n[{sym}] 标记价={mp}  stepSize={step}  ->永续数量={qty}")
            # ② 设杠杆 + 开空
            self.fut.set_leverage(sym, lev)
            r = self.fut.short(sym, qty)
            if r.get("_http") != 200:
                print(f"  永续开空失败: {r}"); continue
            fills = 0.0
            for _ in range(10):          # 等成交
                time.sleep(1.5)
                pos = [p for p in self.fut.positions() if p["symbol"] == sym]
                if pos:
                    fills = abs(float(pos[0]["positionAmt"]))
                    break
            if fills <= 0:
                fills = qty
            print(f"  永续 SHORT {fills} 成交 (order {r.get('orderId')})")
            # ③ 按【永续实际成交数量】买等量现货 → 净敞口严格为 0
            sq, so = self.spot.buy_qty(base, fills)
            print(f"  现货 BUY  {sq} {base} (order {so.get('id')})")
            self.state["positions"][sym] = {
                "perp_qty": fills, "spot_qty": sq, "lev": lev,
                "entry_perp": mp, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.state["opened_at"] = self.state.get("opened_at") or time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()

    # ---------- 状态 ----------
    def status(self):
        """以【我们实际建仓的数量】为准计算净敞口。

        注意：测试网账户里常有**交易所赠送的底仓**（如 1 BTC/1 ETH/大量山寨币），
        若直接用账户余额算净敞口，会把这些底仓误算成我们的多头 → 严重虚报。
        因此这里用 state 里记录的 spot_qty（我们买入的数量）与真实永续持仓配对。
        """
        print("=" * 74)
        print("Carry 状态（净敞口应≈0）")
        print("=" * 74)
        real = {p["symbol"]: p for p in self.fut.positions()}
        state_pos = self.state.get("positions", {})
        if not state_pos and not real:
            print("  （无持仓）"); return 0.0
        total_net = 0.0
        for sym in sorted(set(state_pos) | set(real)):
            base = sym.replace("USDT", "")
            pobj = real.get(sym, {})
            pamt = float(pobj.get("positionAmt", 0) or 0)
            # 优先用 state 记录的自有现货量；无记录则视为 0（不把赠送底仓算进来）
            own_spot = float(state_pos.get(sym, {}).get("spot_qty", 0) or 0)
            net = own_spot + pamt                      # 自有现货多 + 永续空
            px = self.spot.price(base)
            total_net += net * px
            lev = pobj.get("leverage", "-")
            uPnl = pobj.get("unRealizedProfit", "-")
            tag = "" if sym in state_pos else "  (非本策略持仓)"
            print(f"  {sym:10s} 自有现货={own_spot:<12.8g} 永续={pamt:<11.6g} "
                  f"净={net:+.8g} (${net*px:+.2f})  {lev}x  未实现={uPnl}{tag}")
        a = self.fut.account()
        print(f"\n  合约: 钱包={a.get('totalWalletBalance')} 可用={a.get('availableBalance')} "
              f"未实现={a.get('totalUnrealizedProfit')}")
        print(f"  本策略净敞口合计 ≈ ${total_net:+.2f}")
        return total_net

    # ---------- 资金费对账（真实流水）----------
    def funding(self, since_open=True):
        start = None
        if since_open and self.state.get("opened_at"):
            start = int(pd.Timestamp(self.state["opened_at"]).timestamp() * 1000) - 86400000
        inc = self.fut.funding_income(start_ms=start)
        com = self.fut.commission(start_ms=start)
        print("=" * 74)
        print("【真实】资金费对账（来自交易所流水）")
        print("=" * 74)
        if inc.empty:
            print("  暂无资金费记录（资金费每 8 小时结算一次：00/08/16 UTC）")
            return 0.0, 0.0
        for sym, g in inc.groupby("symbol"):
            print(f"  {sym:10s} 笔数={len(g):3d}  累计资金费={g['income'].sum():+.6f} USDT")
        print(f"  ── 资金费合计: {inc['income'].sum():+.6f} USDT")
        if not com.empty:
            print(f"  ── 手续费合计: {com['income'].sum():+.6f} USDT")
        print(f"  ── 净额      : {inc['income'].sum() + (com['income'].sum() if not com.empty else 0):+.6f} USDT")
        return float(inc["income"].sum()), float(com["income"].sum()) if not com.empty else 0.0

    # ---------- 平仓 ----------
    def close(self):
        print("=" * 74); print("平仓（先平永续，再卖现货）"); print("=" * 74)
        pos = {p["symbol"]: float(p["positionAmt"]) for p in self.fut.positions()}
        for sym, st in self.state["positions"].items():
            base = sym.replace("USDT", "")
            pamt = pos.get(sym, 0.0)
            if abs(pamt) > 1e-12:                  # 空头为负 → 买回
                code, body = self.fut.close_short(sym, abs(pamt))
                print(f"  {sym:10s} 平永续 {abs(pamt)} -> [{code}] {body[:90]}")
            free = self.spot.free(base)
            if free > 0:
                q, o = self.spot.sell_qty(base, free)
                print(f"  {sym:10s} 卖现货 {q} -> {o.get('id')}")
        self.state["positions"] = {}
        self._save()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    c = Carry()
    if cmd == "status":
        c.status()
    elif cmd == "funding":
        c.funding()
    elif cmd == "open":
        c.open(sys.argv[2:], notional_each=100.0, lev=2)
        c.status()
    elif cmd == "close":
        c.close()
    elif cmd == "auto":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        import crypto_funding as CF
        fd = CF.funding_daily()
        picks = list(fd.iloc[-1].dropna().sort_values(ascending=False).head(n).index)
        print(f"按最新资金费率自动选出: {picks}")
        c.open(picks, notional_each=100.0, lev=2)
        c.status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
