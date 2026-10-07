"""
webapp.py - 量化回测平台（浏览器看板）
====================================
在本地起一个网页，像软件平台一样用鼠标点：选因子/参数 → 回测 / 挖alpha / 评分 →
看图表 + 指标，并把达标因子存进 Alpha 库（质量门槛）。

启动：python webapp.py   →  浏览器打开 http://127.0.0.1:5050
"""

import os
import io

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

import run as R                     # 复用 load_prices / compute_factor / FACTORS
from factor import evaluate_factor, factor_to_weights
from backtest import portfolio_backtest, buy_and_hold
from metrics import summary
from alpha_score import score_factor
from alpha_library import (save, remove, clear, list_alphas, meets_threshold,
                           pool_add, pool_list, pool_rm)

PLOT_DIR = "output_dash"
os.makedirs(PLOT_DIR, exist_ok=True)

app = Flask(__name__)


# ---------------- 辅助 ----------------

def _load():
    return R.load_prices()


def _run_strategy(factor_name, lookback, top, period, long_short, hold=1):
    """预设/基本面/条件因子入口，交给通用 _eval_factor。"""
    prices = _load()
    f = _get_factor(prices, factor_name, lookback)
    if hold is None and factor_name == "capitulation":
        hold = 10   # 条件反转调优后的持有期
    return _eval_factor(prices, f, top, period, long_short,
                        name=f"{factor_name}_{lookback}", hold=hold or 1)


def _eval_factor(prices, factor, top, period, long_short, name, hold=1):
    """通用：给定因子面板，回测 + 评分 + IC + 画图（快）。hold>1 用持有期重平衡。"""
    from analysis import plot_equity_curve, plot_drawdown
    if hold and hold > 1:
        from alphas import factor_to_weights_hold
        w = factor_to_weights_hold(factor, top_pct=top, long_only=not long_short, hold=hold)
    else:
        w = factor_to_weights(factor, top_pct=top, long_only=not long_short)
    net, turnover = portfolio_backtest(prices, w)
    bench = buy_and_hold(prices)
    s = summary(net)
    score = score_factor(prices, factor, top_pct=top, long_only=not long_short, hold=hold)
    icr = evaluate_factor(factor, prices, period=period, name=name, quiet=True)

    eq = (1.0 + net).cumprod()
    beq = (1.0 + bench).cumprod()
    fname = f"{name}.png"
    plot_equity_curve(net, bench, f"{name}", f"{PLOT_DIR}/{fname}")
    plot_drawdown(net, f"{name} drawdown", f"{PLOT_DIR}/dd_{fname}")

    return {
        "metrics": {
            "total_return": s["total_return"],
            "annualized_return": s["annualized_return"],
            "annualized_vol": s["annualized_vol"],
            "sharpe": s["sharpe"],
            "max_drawdown": s["max_drawdown"],
            "calmar": s["calmar"],
            "win_rate": s["win_rate"],
            "bench_annualized": summary(bench)["annualized_return"],
        },
        "wq_score": score,
        "ic": {"ic_mean": icr["ic_mean"], "icir": icr["icir"]},
        "equity": {
            "dates": [str(d.date()) for d in eq.index],
            "strategy": [round(float(x), 4) for x in eq.values],
            "benchmark": [round(float(x), 4) for x in beq.values],
        },
        "plot_url": f"/plots/{fname}",
        "dd_plot_url": f"/plots/dd_{fname}",
        "name": name,
    }


def _compute_significance(prices, factor, period, top, long_short, hold=1):
    """单独计算显著性（FM + FF alpha），较慢，供异步调用。"""
    from research import factor_significance
    from stats import ff_alpha
    try:
        fm = factor_significance(factor, prices, period=period)
        fm_res = {"tstat": fm.get("nw_tstat"), "p": fm.get("p_value"),
                  "significant": bool(fm.get("p_value", 1) < 0.05),
                  "ok": bool(fm.get("ok", False))}
    except Exception:
        fm_res = {"ok": False}
    try:
        if hold and hold > 1:
            from alphas import factor_to_weights_hold
            w = factor_to_weights_hold(factor, top_pct=top, long_only=not long_short, hold=hold)
        else:
            w = factor_to_weights(factor, top_pct=top, long_only=not long_short)
        net, _ = portfolio_backtest(prices, w)
        bench = buy_and_hold(prices)
        a = ff_alpha(net, bench)
        alpha_res = {"alpha": a["params"]["alpha"], "alpha_t": a["tstats"]["alpha"],
                     "beta_mkt": float(a["params"].get("MKT")), "r2": a["r2"],
                     "significant": bool(abs(a["tstats"]["alpha"]) > 2)}
    except Exception:
        alpha_res = {"ok": False}
    return {"fm": fm_res, "ff_alpha": alpha_res}


def _get_factor(prices, factor_name, lookback, code=None):
    """按名称或用户代码得到因子面板。"""
    if code:
        import factor as F
        ns = {"prices": prices, "pd": pd, "np": np, "F": F}
        # 暴露 OHLCV，供用户写需要开高低量的因子
        try:
            from data_ohlcv import load_ohlcv_panels
            ns["OHLCV"] = load_ohlcv_panels(symbols=list(prices.columns))
        except Exception:
            ns["OHLCV"] = None
        exec(code, ns)
        f = ns["factor"](prices)
        return f.reindex(prices.index).reindex(prices.columns, axis=1)
    if factor_name == "capitulation":
        from alphas import capitulation_reversal
        f = capitulation_reversal(symbols=list(prices.columns))
        return f.reindex(prices.index).reindex(prices.columns, axis=1)
    FUND_FACTORS = ["bm", "size", "roe", "roa", "investment", "profit_growth"]
    if factor_name in FUND_FACTORS:
        from fundamentals import build_factor_panels
        panels = build_factor_panels(prices, symbols=list(prices.columns),
                                     factors=[factor_name], progress=False,
                                     cached_only=True)
        return panels[factor_name]
    return R.compute_factor(prices, factor_name, lookback)


def _run_custom(code, top, period, long_short):
    """执行用户写的 Python 因子代码，得到因子面板（或权重）再回测。"""
    prices = _load()
    import factor as F
    ns = {"prices": prices, "pd": pd, "np": np}
    # 注入因子算子库，方便用户直接用 F.momentum / F.zscore / ...
    ns["F"] = F
    try:
        exec(code, ns)
    except Exception as e:
        return {"ok": False, "error": f"代码执行错误: {type(e).__name__}: {e}"}
    try:
        if "weights" in ns and callable(ns["weights"]):
            w = ns["weights"](prices)
            # 权重模式：直接回测（仍算评分需因子，用权重排名近似）
            net, turnover = portfolio_backtest(prices, w)
            bench = buy_and_hold(prices)
            s = summary(net)
            eq = (1.0 + net).cumprod(); beq = (1.0 + bench).cumprod()
            return {"ok": True, "name": "custom_weights",
                    "metrics": {"total_return": s["total_return"],
                                "annualized_return": s["annualized_return"],
                                "annualized_vol": s["annualized_vol"],
                                "sharpe": s["sharpe"], "max_drawdown": s["max_drawdown"],
                                "calmar": s["calmar"], "win_rate": s["win_rate"],
                                "bench_annualized": summary(bench)["annualized_return"]},
                    "equity": {"dates": [str(d.date()) for d in eq.index],
                               "strategy": [round(float(x),4) for x in eq.values],
                               "benchmark": [round(float(x),4) for x in beq.values]},
                    "wq_score": None, "ic": {"ic_mean": 0.0, "icir": 0.0},
                    "plot_url": "", "dd_plot_url": ""}
        if "factor" not in ns or not callable(ns["factor"]):
            return {"ok": False, "error": "代码里没有定义 factor(prices) 或 weights(prices) 函数"}
        f = ns["factor"](prices)
        return {"ok": True, **(_eval_factor(prices, f, top, period, long_short,
                                            name="custom_factor"))}
    except Exception as e:
        return {"ok": False, "error": f"运行出错: {type(e).__name__}: {e}"}


# ---------------- API ----------------

@app.route("/")
def index():
    resp = send_from_directory(".", "dashboard.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/plots/<path:name>")
def plots(name):
    return send_from_directory(PLOT_DIR, name)


@app.route("/api/data")
def api_data():
    prices = _load()
    fund = ["bm", "size", "roe", "roa", "investment", "profit_growth"]
    return jsonify({
        "rows": int(prices.shape[0]), "cols": int(prices.shape[1]),
        "start": str(prices.index[0].date()), "end": str(prices.index[-1].date()),
        "factors": list(R.FACTORS.keys()) + fund + ["capitulation"],
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    d = request.get_json(force=True)
    factor = d["factor"]; lb = int(d.get("lookback", 60))
    top = float(d.get("top", 0.2)); period = int(d.get("period", 5))
    long_short = bool(d.get("long_short", False))
    hold = d.get("hold")          # 缺省时 capitulation 自动用 10
    try:
        res = _run_strategy(factor, lb, top, period, long_short, hold)
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/run_custom", methods=["POST"])
def api_run_custom():
    """执行用户自定义 Python 因子代码并回测。"""
    d = request.get_json(force=True)
    code = d.get("code", "")
    top = float(d.get("top", 0.2)); period = int(d.get("period", 5))
    long_short = bool(d.get("long_short", False))
    return jsonify(_run_custom(code, top, period, long_short))


@app.route("/api/significance", methods=["POST"])
def api_significance():
    """异步计算显著性（Fama-MacBeth + FF alpha），较慢。"""
    d = request.get_json(force=True)
    code = d.get("code")
    factor_name = d.get("factor", "momentum")
    lb = int(d.get("lookback", 60))
    top = float(d.get("top", 0.2)); period = int(d.get("period", 5))
    long_short = bool(d.get("long_short", False))
    hold = d.get("hold")
    if hold is None and factor_name == "capitulation":
        hold = 10
    prices = _load()
    try:
        f = _get_factor(prices, factor_name, lb, code)
        res = _compute_significance(prices, f, period, top, long_short, hold=hold or 1)
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/libsave", methods=["POST"])
def api_libsave():
    d = request.get_json(force=True)
    factor = d["factor"]; lb = int(d.get("lookback", 60))
    top = float(d.get("top", 0.2)); period = int(d.get("period", 5))
    min_grade = d.get("min_grade", "B"); name = d.get("name")
    long_short = bool(d.get("long_short", False))
    prices = _load()
    f = _get_factor(prices, factor, lb)
    return _try_save(f, prices, top, period, min_grade, name, factor, lb, long_short)


@app.route("/api/libsave_custom", methods=["POST"])
def api_libsave_custom():
    d = request.get_json(force=True)
    code = d.get("code", "")
    top = float(d.get("top", 0.2)); period = int(d.get("period", 5))
    min_grade = d.get("min_grade", "B"); name = d.get("name") or "custom"
    long_short = bool(d.get("long_short", False))
    prices = _load()
    import factor as F
    ns = {"prices": prices, "pd": pd, "np": np, "F": F}
    try:
        exec(code, ns)
        f = ns["factor"](prices)
    except Exception as e:
        return jsonify({"ok": False, "saved": False, "reason": f"代码错误: {e}"})
    return jsonify(_try_save(f, prices, top, period, min_grade, name,
                             "custom", 0, long_short))


def _try_save(f, prices, top, period, min_grade, name, factor, lb, long_short):
    sc = score_factor(prices, f, top_pct=top, long_only=not long_short)
    icr = evaluate_factor(f, prices, period=period, name=name, quiet=True)
    grade = sc["grade"]
    if meets_threshold(grade, min_grade):
        rid = save({
            "name": name or f"{factor}_{lb}", "factor": factor, "lookback": lb,
            "top_pct": top, "long_only": not long_short,
            "grade": grade, "good": sc["good"], "sharpe": sc["sharpe"],
            "fitness": sc["fitness"], "returns": sc["returns"],
            "turnover": sc["turnover"], "drawdown": sc["drawdown"],
            "margin": sc["margin"], "conc": sc["weight_concentration"],
            "ic": icr["ic_mean"], "icir": icr["icir"],
        })
        return {"ok": True, "saved": True, "id": rid, "grade": grade}
    return {"ok": True, "saved": False, "grade": grade,
            "reason": f"等级{grade}未达门槛{min_grade}，已丢弃"}


@app.route("/api/liblist")
def api_liblist():
    rows = list_alphas()
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/librm", methods=["POST"])
def api_librm():
    d = request.get_json(force=True)
    ok = remove(int(d["id"]))
    return jsonify({"ok": True, "removed": ok})


@app.route("/api/libclear", methods=["POST"])
def api_libclear():
    n = clear()
    return jsonify({"ok": True, "cleared": n})


@app.route("/api/pool_add", methods=["POST"])
def api_pool_add():
    """记录一个测试过的候选 alpha 到持久候选池（BRAIN 风格）。"""
    d = request.get_json(force=True)
    rid = pool_add(
        name=d.get("name", "candidate"), factor=d.get("factor"),
        lookback=d.get("lookback"), expression=d.get("expression"),
        is_custom=bool(d.get("is_custom")), grade=d.get("grade"),
        good=bool(d.get("good")), sharpe=d.get("sharpe"), fitness=d.get("fitness"),
        returns=d.get("returns"), turnover=d.get("turnover"),
        subU=d.get("subU"), ic=d.get("ic"))
    return jsonify({"ok": True, "id": rid})


@app.route("/api/pool_save", methods=["POST"])
def api_pool_save():
    """把候选池里一条达标(GOOD)的记录存进 Alpha 库。"""
    d = request.get_json(force=True)
    pid = int(d["id"])
    rows = pool_list()
    rec = next((r for r in rows if r["id"] == pid), None)
    if rec is None:
        return jsonify({"ok": False, "saved": False, "reason": "记录不存在"})
    if not rec.get("good"):
        return jsonify({"ok": False, "saved": False, "reason": "未达标(GOOD)，不存"})
    prices = _load()
    if rec.get("is_custom") and rec.get("expression"):
        code = rec["expression"]
        ns = {"prices": prices, "pd": pd, "np": np}
        import factor as F; ns["F"] = F
        exec(code, ns)
        f = ns["factor"](prices).reindex(prices.index).reindex(prices.columns, axis=1)
    elif rec.get("factor"):
        f = _get_factor(prices, rec["factor"], rec.get("lookback") or 60)
    else:
        return jsonify({"ok": False, "saved": False, "reason": "缺因子信息"})
    return jsonify(_try_save(f, prices, 0.2, 5, "B", rec["name"],
                             rec.get("factor") or "pool", rec.get("lookback") or 0, False))


@app.route("/api/pool_list")
def api_pool_list():
    return jsonify({"ok": True, "rows": pool_list()})


@app.route("/api/pool_rm", methods=["POST"])
def api_pool_rm():
    d = request.get_json(force=True)
    return jsonify({"ok": True, "removed": pool_rm(int(d["id"]))})


def _datacats():
    """中国 A股 数据类别注册表（平台确认的数据源状态）。"""
    import analyst_data as AD
    cats = []

    # 1 价量
    try:
        p = _load()
        cats.append({"category": "价量 (price/volume)", "source": "akshare(东财/sina)/data/a_share_close.csv",
                     "status": "wired", "detail": f"{p.shape[0]}天 × {p.shape[1]}只", "history": True})
    except Exception as e:
        cats.append({"category": "价量", "source": "a_share_close.csv", "status": f"err:{e}"})

    # OHLCV
    try:
        from data_ohlcv import list_available
        n = len(list_available())
        cats.append({"category": "OHLCV 开高低量", "source": "akshare/data/ohlcv", "status": "wired",
                     "detail": f"{n}只", "history": True})
    except Exception as e:
        cats.append({"category": "OHLCV", "source": "data/ohlcv", "status": f"err:{e}"})

    # 2 财务
    try:
        import glob
        nf = len(glob.glob("data/fundamentals/*.csv"))
        cats.append({"category": "财务 (bm/size/roe/roa/投资/盈利增速)", "source": "akshare stock_financial_abstract",
                     "status": "wired", "detail": f"缓存{nf}只", "history": True})
    except Exception as e:
        cats.append({"category": "财务", "source": "akshare", "status": f"err:{e}"})

    # 3 分析师一致预期
    try:
        dfa = AD.load_consensus()   # 读缓存，不强制联网
        if dfa is None or dfa.empty:
            cats.append({"category": "分析师一致预期", "source": "akshare stock_profit_forecast_em",
                         "status": "pending", "detail": "无缓存(需先抓取)"})
        else:
            fwd_y = int(dfa["fwd_year"].iloc[0]) if "fwd_year" in dfa else "?"
            cats.append({"category": "分析师一致预期 (覆盖/评级/前瞻EPS)", "source": "akshare 东财业绩预测",
                         "status": "cached", "detail": f"{len(dfa)}只, 前瞻FY{fwd_y}",
                         "history": False, "note": "当前截面(非历史时序)"})
    except Exception as e:
        cats.append({"category": "分析师一致预期", "source": "akshare", "status": f"err:{e}"})

    # 4 新闻情绪
    try:
        import news_data as ND
        ndf = ND.load_news(cached_only=True)   # 仅缓存，不联网
        if ndf is None or ndf.empty:
            cats.append({"category": "新闻情绪 (per-stock news)", "source": "akshare stock_news_em",
                         "status": "available", "detail": "逐只拉取(未缓存)", "history": False})
        else:
            cats.append({"category": "新闻情绪 (个股近闻+词典情感)", "source": "akshare 东财个股新闻",
                         "status": "cached", "detail": f"{len(ndf)}条/{ndf['code'].nunique()}只",
                         "history": False, "note": "近10条/股 快照，词典启发式情绪(非BERT)"})
    except Exception as e:
        cats.append({"category": "新闻情绪", "source": "akshare", "status": f"err:{e}"})
    return cats


@app.route("/api/datacats")
def api_datacats():
    return jsonify({"ok": True, "categories": _datacats()})


@app.route("/api/analyst", methods=["GET"])
def api_analyst():
    """返回分析师一致预期快照（按覆盖广度排序，附前瞻盈利收益率）。"""
    import analyst_data as AD
    try:
        dfa = AD.load_consensus()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    if dfa is None or dfa.empty:
        return jsonify({"ok": True, "rows": [], "note": "无分析师一致预期缓存，先跑 analyst_data 抓取"})
    # 附前瞻盈利收益率（用平台价格面板最新价）；merge 处理重复 code
    try:
        p = _load()
        latest = p.iloc[-1]
        latest.index = latest.index.astype(str).str.zfill(6)
        fy = AD.forward_earnings_yield(dfa, latest)
        if fy is not None and not fy.empty:
            fyf = fy.rename("fwd_yield").reset_index().rename(columns={"index": "code"})
            dfa = dfa.merge(fyf, on="code", how="left")
    except Exception as e:
        print(f"[analyst] fwd_yield merge err: {e}")
    keep = ["code", "name", "n_report", "n_buy", "buy_ratio", "fwd_eps", "fwd_year", "fwd_yield"]
    keep = [c for c in keep if c in dfa.columns]
    rows = dfa[keep].sort_values("n_report", ascending=False).head(100)
    rows = rows.where(rows.notna(), None)
    return jsonify({"ok": True, "rows": rows.to_dict("records"),
                    "total": int(len(dfa)), "fwd_year": int(dfa["fwd_year"].iloc[0]) if "fwd_year" in dfa else None})


@app.route("/api/news")
def api_news():
    """返回个股新闻情绪快照：聚合 + 近期标题。可选 ?refetch=1 重新抓取。"""
    import news_data as ND
    refetch = request.args.get("refetch", "0") == "1"
    try:
        ndf = ND.load_news(refetch=refetch, cached_only=not refetch)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    if ndf is None or ndf.empty:
        return jsonify({"ok": True, "rows": [], "agg": [], "note": "无新闻缓存，先抓取"})
    agg = ND.aggregate_sentiment(ndf).to_dict("records")
    # 最近标题
    dfc = ndf.copy()
    if "time" in dfc.columns:
        dfc = dfc.sort_values("time", ascending=False)
    recent = dfc.head(60)
    recent = recent.replace({np.nan: None})
    rows = recent.to_dict("records")
    return jsonify({"ok": True, "rows": rows, "agg": agg, "total": int(len(ndf))})


@app.route("/api/ic_screen", methods=["POST"])
def api_ic_screen():
    """横向 IC 筛选：对比各因子在多个预测期的 IC/ICIR。用子样本加速。"""
    from factor import factor_ic, forward_returns
    d = request.get_json(force=True) or {}
    periods = d.get("periods", [5, 21])
    factors = d.get("factors") or None
    prices = _load()
    # 子采样加速：取 ~300 只
    if prices.shape[1] > 300:
        step = max(1, prices.shape[1] // 300)
        prices = prices.iloc[:, ::step]
    names = factors if factors else list(R.FACTORS.keys()) + ["bm", "size", "roe", "investment"]
    rows = []
    for fn in names:
        try:
            f = _get_factor(prices, fn, 60).reindex(prices.index).reindex(prices.columns, axis=1)
        except Exception:
            continue
        for p in periods:
            ic = factor_ic(f, forward_returns(prices, p))
            ic = ic.replace([np.inf, -np.inf], np.nan).dropna()
            if len(ic) < 20:
                continue
            rows.append({"factor": fn, "period": p,
                         "IC": round(float(ic.mean()), 4),
                         "ICIR": round(float(ic.mean() / ic.std()), 4) if ic.std() > 0 else 0})
    return jsonify({"ok": True, "rows": rows})


# ===========================================================================
# 加密货币量化板块（新增；A股功能完全保留）
# ===========================================================================
PPY_CRYPTO = 365

CRYPTO_FACTORS = ["momentum", "reversal", "price_ma", "volatility", "volume"]


def _crypto_prices():
    import crypto_mine as CM
    return CM.load_crypto_prices("1d")


def _crypto_factor(prices, name, lookback):
    import factor as F
    if name == "momentum":   return F.momentum(prices, lookback)
    if name == "reversal":   return F.short_term_reversal(prices, lookback)
    if name == "price_ma":   return F.price_vs_ma(prices, lookback)
    if name == "volatility": return F.volatility_factor(prices, lookback)
    if name == "volume":     return F.volume_factor(prices)
    raise ValueError(f"unknown crypto factor {name}")


@app.route("/crypto")
def crypto_index():
    resp = send_from_directory(".", "crypto_dashboard.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/crypto/meta")
def api_crypto_meta():
    from crypto_data import list_available, load_symbol
    av = list_available()
    coins = sorted(av.get("1d", []))
    info = []
    for c in coins:
        try:
            d = load_symbol(c, "1d")
            if len(d):
                info.append({"code": c, "rows": int(len(d)),
                             "start": str(d.index.min().date()), "end": str(d.index.max().date())})
        except Exception:
            pass
    p = _crypto_prices()
    return jsonify({"ok": True, "coins": info, "n_coins": len(coins),
                    "intervals": {k: len(v) for k, v in av.items()},
                    "panel": {"rows": int(p.shape[0]), "cols": int(p.shape[1]),
                              "start": str(p.index.min().date()) if len(p) else None,
                              "end": str(p.index.max().date()) if len(p) else None},
                    "factors": CRYPTO_FACTORS})


@app.route("/api/crypto/run", methods=["POST"])
def api_crypto_run():
    from analysis import plot_equity_curve, plot_drawdown
    from factor import factor_to_weights, zscore
    from backtest import portfolio_backtest, buy_and_hold
    from metrics import summary
    d = request.get_json(force=True)
    name = d.get("factor", "momentum")
    lb = int(d.get("lookback", 20))
    top = float(d.get("top", 0.2))
    hold = int(d.get("hold", 1))
    long_short = bool(d.get("long_short", True))
    prices = _crypto_prices()
    if prices.empty:
        return jsonify({"ok": False, "error": "无加密货币数据，请先运行 crypto_data.fetch_all()"})
    try:
        f = _crypto_factor(prices, name, lb)
        f = zscore(f).reindex(prices.index).reindex(prices.columns, axis=1)
        sc = score_factor(prices, f, top_pct=top, long_only=not long_short,
                          commission_bps=10, slippage_bps=5, hold=hold, ppy=PPY_CRYPTO)
        if hold and hold > 1:
            from alphas import factor_to_weights_hold
            w = factor_to_weights_hold(f, top_pct=top, long_only=not long_short, hold=hold)
        else:
            w = factor_to_weights(f, top_pct=top, long_only=not long_short)
        w = w.reindex(prices.index).fillna(0.0)
        net, _ = portfolio_backtest(prices, w)
        bench = buy_and_hold(prices)
        eq = (1.0 + net).cumprod(); beq = (1.0 + bench).cumprod()
        tag = f"crypto_{name}_{lb}"
        plot_equity_curve(net, bench, tag, f"{PLOT_DIR}/{tag}.png")
        plot_drawdown(net, f"{tag} drawdown", f"{PLOT_DIR}/dd_{tag}.png")
        return jsonify({"ok": True, "name": tag, "wq_score": sc,
                        "bench_ann": summary(bench)["annualized_return"],
                        "equity": {"dates": [str(x.date()) for x in eq.index],
                                   "strategy": [round(float(v), 4) for v in eq.values],
                                   "benchmark": [round(float(v), 4) for v in beq.values]},
                        "plot_url": f"/plots/{tag}.png", "dd_plot_url": f"/plots/dd_{tag}.png"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/crypto/screen", methods=["POST"])
def api_crypto_screen():
    import crypto_mine as CM
    prices = _crypto_prices()
    if prices.empty:
        return jsonify({"ok": False, "error": "无加密货币数据"})
    d = request.get_json(force=True) or {}
    periods = d.get("periods", [1, 5, 20, 60])
    try:
        best = CM.screen(prices, periods=tuple(periods))
        return jsonify({"ok": True, "rows": best.to_dict("records")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/crypto/pool_add", methods=["POST"])
def api_crypto_pool_add():
    import alpha_library as L
    d = request.get_json(force=True)
    rid = L.pool_add(name=d.get("name", "crypto"), factor=d.get("factor"),
                     lookback=d.get("lookback"), expression=d.get("expression"),
                     is_custom=False, grade=d.get("grade"), good=bool(d.get("good")),
                     sharpe=d.get("sharpe"), fitness=d.get("fitness"),
                     returns=d.get("returns"), turnover=d.get("turnover"),
                     subU=d.get("subU"), ic=d.get("ic"))
    return jsonify({"ok": True, "id": rid})


if __name__ == "__main__":
    print("量化回测平台已启动:  http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
