"""
crypto_exec.py - 加密货币交易接入（模拟盘 / 纸面交易）
========================================================
支持三种模式，**默认最安全的 paper**：
  paper   : 纸面撮合（用本地行情模拟成交，不下任何单）—— 无需 API Key
  testnet : 交易所测试网（假钱、真实撮合）—— 需你注册 testnet API Key
  live    : 真实盘 —— 需显式危险开关，默认禁用

配置：data/crypto/exchange.json
  {
    "exchange": "binance",         # binance / okx / bybit
    "testnet": true,
    "apiKey": "",
    "secret": "",
    "password": ""                 # OKX 需要
  }

用法：
    from crypto_exec import load_config, connect, get_balance, paper_order
    cfg = load_config()
    ex  = connect(cfg)             # testnet 无 key 会返回 None 并给出指引
    get_balance(ex)
"""

import os
import json
import time

import pandas as pd

try:  # ccxt 装在 vendor 目录
    import sys
    _v = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    if os.path.isdir(_v) and _v not in sys.path:
        sys.path.insert(0, _v)
    import ccxt
    HAS_CCXT = True
except Exception:
    HAS_CCXT = False

CONFIG = os.path.join("data", "crypto", "exchange.json")

# 各交易所测试网说明（供你注册时参考）
TESTNET_HELP = {
    "binance": "https://testnet.binance.vision/  (Spot Testnet，免注册GitHub登录即可拿 Key)",
    "okx":     "https://www.okx.com/  (账户 → 模拟交易 → 生成模拟盘 API Key；请求需带 x-simulated-trading:1)",
    "bybit":   "https://testnet.bybit.com/  (注册测试网账号 → API 管理 → 创建测试网 Key)",
}


def load_config() -> dict:
    if os.path.exists(CONFIG):
        try:
            return json.load(open(CONFIG, encoding="utf-8"))
        except Exception:
            pass
    cfg = {"exchange": "binance", "testnet": True, "apiKey": "", "secret": "", "password": ""}
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cfg


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def connect(cfg: dict = None):
    """按配置连接交易所。testnet 无 Key 时返回 None（并打印注册指引）。"""
    cfg = cfg or load_config()
    if not HAS_CCXT:
        print("[exec] 未安装 ccxt（vendor/ 下），无法连接交易所")
        return None
    name = (cfg.get("exchange") or "binance").lower()
    if not cfg.get("apiKey") or not cfg.get("secret"):
        print(f"[exec] 缺少 API Key。请到测试网注册后填入 {CONFIG}")
        print(f"[exec] {name} 测试网：{TESTNET_HELP.get(name,'')}")
        return None
    klass = getattr(ccxt, name)
    ex = klass({"apiKey": cfg["apiKey"], "secret": cfg["secret"],
                "password": cfg.get("password") or None,
                "enableRateLimit": True, "timeout": 20000})
    try:
        if cfg.get("testnet", True):
            ex.set_sandbox_mode(True)
        ex.load_markets()
        print(f"[exec] 已连接 {name} (testnet={cfg.get('testnet',True)})")
        return ex
    except Exception as e:
        print(f"[exec] 连接失败: {e}")
        return None


def get_balance(ex):
    if ex is None:
        return None
    try:
        b = ex.fetch_balance()
        tot = {k: v for k, v in (b.get("total") or {}).items() if v and abs(v) > 1e-12}
        print("[exec] 余额:", tot if tot else "(空)")
        return tot
    except Exception as e:
        print("[exec] 取余额失败:", e)
        return None


def get_price(symbol="BTC/USDT", ex=None):
    """有交易所用交易所价，否则回退本地 CSV 最新价。"""
    if ex is not None:
        try:
            return float(ex.fetch_ticker(symbol)["last"])
        except Exception:
            pass
    from crypto_data import load_symbol
    code = symbol.replace("/", "")
    df = load_symbol(code, "1d")
    if len(df):
        return float(df["close"].iloc[-1])
    return None


def paper_order(symbol, side, amount, price=None, fee_bps=10.0, ex=None):
    """纸面成交（不触发任何真实下单）。返回成交回执 dict。"""
    px = price or get_price(symbol, ex)
    if px is None:
        return {"ok": False, "error": "无价格"}
    notional = px * amount
    fee = notional * fee_bps / 10000.0
    rec = {"ok": True, "mode": "paper", "symbol": symbol, "side": side,
           "amount": amount, "price": round(px, 8), "notional": round(notional, 2),
           "fee": round(fee, 4), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"[paper] {side} {amount} {symbol} @ {px:.4f}  名义={notional:.2f} 费={fee:.4f}")
    _log(rec)
    return rec


def place_order(ex, symbol, side, amount, order_type="market", price=None, allow_live=False):
    """真实/testnet 下单。live 需显式 allow_live=True（默认安全禁用）。"""
    cfg = load_config()
    if not allow_live and not cfg.get("testnet", True):
        return {"ok": False, "error": "实盘下单被禁用（需 allow_live=True 且自行承担风险）"}
    if ex is None:
        return {"ok": False, "error": "未连接交易所"}
    try:
        o = ex.create_order(symbol, order_type, side, amount, price)
        print("[exec] 下单成功:", o.get("id"))
        _log({"ok": True, "mode": "testnet" if cfg.get("testnet", True) else "live",
              "symbol": symbol, "side": side, "amount": amount,
              "price": o.get("price"), "id": o.get("id"),
              "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        return {"ok": True, "order": o}
    except Exception as e:
        print("[exec] 下单失败:", e)
        return {"ok": False, "error": str(e)}


def _log(rec: dict):
    p = os.path.join("data", "crypto", "orders.log")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    cfg = load_config()
    print("配置:", cfg)
    print("ccxt:", HAS_CCXT)
    ex = connect(cfg)
    if ex:
        get_balance(ex)
    else:
        print("\n--- 纸面模式自测（无需 Key）---")
        paper_order("BTC/USDT", "buy", 0.001)
