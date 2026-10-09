"""Chronological, market-level validation of Polymarket sports calibration.

Run from any directory with the project's Python environment:
    python modeling/model_calibration.py

Uses market end dates from the original metadata. It deliberately excludes
lifetime volume and n_points from predictive features because they are only
known after a market closes. Standard-library CSV/JSON plus numpy, pandas,
and scipy are required.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
SPORTS = Path(r"D:\26050\Documents\polymarket_sports")
DATA = ROOT / "modeling" / "historical_rows.csv"
OUT = ROOT / "modeling" / "model_results"
FEE_RATE = 0.05
HALF_SPREAD = 0.005
BOOTSTRAPS = 1000
SEED = 20261008


def load_end_dates(ids: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (SPORTS / "data" / "markets").glob("*.json"):
        try:
            markets = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in markets:
            mid = str(m.get("market_id", ""))
            if mid in ids:
                date = str(m.get("end_date") or m.get("closed_time") or "")[:10]
                if date:
                    found[mid] = date
        if len(found) == len(ids):
            break
    return found


def design(frame: pd.DataFrame, type_levels: list[str], league_levels: list[str]) -> np.ndarray:
    p = frame.price.to_numpy(float).clip(1e-5, 1 - 1e-5)
    z = np.log(p / (1 - p))
    # Smooth calibration curve plus a small set of known-at-decision-time controls.
    base = pd.DataFrame({
        "intercept": 1.0,
        "logit_price": z,
        "logit_price_sq": z * z,
        "lead_days": frame.lead_days.to_numpy(float) / 30.0,
        "neg_risk": frame.neg_risk.to_numpy(float),
    }, index=frame.index)
    typ = pd.Categorical(frame.sports_market_type.fillna(""), categories=type_levels)
    lg = pd.Categorical(frame.league_name.fillna(""), categories=league_levels)
    td = pd.get_dummies(typ, prefix="type", dtype=float)
    ld = pd.get_dummies(lg, prefix="league", dtype=float)
    # Fixed effects are ridge-regularized during fitting; omit first level.
    if td.shape[1] > 1:
        td = td.iloc[:, 1:]
    if ld.shape[1] > 1:
        ld = ld.iloc[:, 1:]
    return np.column_stack([base.to_numpy(float), td.to_numpy(float), ld.to_numpy(float)])


def fit_logit(x: np.ndarray, y: np.ndarray, penalty_start: int) -> np.ndarray:
    def objective(b):
        eta = np.clip(x @ b, -30, 30)
        loss = np.logaddexp(0, eta).sum() - np.dot(y, eta)
        reg = 0.5 * 2.0 * np.dot(b[penalty_start:], b[penalty_start:])
        grad = x.T @ (1 / (1 + np.exp(-eta)) - y)
        grad[penalty_start:] += 2.0 * b[penalty_start:]
        return loss + reg, grad
    result = minimize(objective, np.zeros(x.shape[1]), jac=True, method="L-BFGS-B")
    if not result.success and not np.isfinite(result.fun):
        raise RuntimeError("Logistic model did not converge: " + result.message)
    return result.x


def metric(y: np.ndarray, pred: np.ndarray) -> tuple[float, float]:
    return float(np.mean((y - pred) ** 2)), float(np.mean(np.abs(y - pred)))


def bh_adjust(pvals: list[float]) -> list[float]:
    p = np.asarray(pvals, float)
    order = np.argsort(p)
    adjusted = np.empty(len(p))
    running = 1.0
    for rank_idx in range(len(p) - 1, -1, -1):
        i = order[rank_idx]
        running = min(running, p[i] * len(p) / (rank_idx + 1))
        adjusted[i] = running
    return adjusted.tolist()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    df = pd.read_csv(DATA, keep_default_na=False, low_memory=False)
    df["outcome"] = df.outcome.astype(int)
    df["price"] = df.price.astype(float)
    df["lead_days"] = df.lead_days.astype(float)
    df["neg_risk"] = df.neg_risk.astype(float)
    df["sports_market_type"] = df.sports_market_type.fillna("")
    ids = set(df.market_id.astype(str))
    end_dates = load_end_dates(ids)
    df["end_date"] = df.market_id.astype(str).map(end_dates)
    df = df[df.end_date.notna()].copy()
    df["end_date"] = pd.to_datetime(df.end_date, errors="coerce", utc=True)
    df = df[df.end_date.notna()].copy()
    market_dates = df[["market_id", "end_date"]].drop_duplicates().sort_values("end_date")
    cutoff_idx = min(max(int(len(market_dates) * 0.8), 1), len(market_dates) - 1)
    cutoff = market_dates.iloc[cutoff_idx].end_date
    train = df[df.end_date < cutoff].copy()
    test = df[df.end_date >= cutoff].copy()
    # Collapse duplicate cutoff-date effects by retaining all markets at/after date.
    type_levels = sorted(df.sports_market_type.unique().tolist())
    league_levels = sorted(df.league_name.unique().tolist())
    xtr = design(train, type_levels, league_levels)
    xte = design(test, type_levels, league_levels)
    # First five columns are unpenalized continuous/base terms.
    beta = fit_logit(xtr, train.outcome.to_numpy(float), 5)
    pred = 1 / (1 + np.exp(-np.clip(xte @ beta, -30, 30)))
    y = test.outcome.to_numpy(float)
    p = test.price.to_numpy(float)
    brier_model, mae_model = metric(y, pred)
    brier_market, mae_market = metric(y, p)

    # One action per eligible market/lead observation. Cluster bootstrap by market.
    fee = FEE_RATE * p * (1 - p)
    cost = fee + HALF_SPREAD
    yes_edge = pred - p - cost
    no_edge = p - pred - cost
    # Use one prespecified entry time so six observations of one market do not
    # masquerade as six independent opportunities.
    entry = test.lead_days.to_numpy(float) == 3
    side = np.where(entry & (yes_edge > 0), 1, np.where(entry & (no_edge > 0), -1, 0))
    selected_net = np.where(side == 1, yes_edge, np.where(side == -1, no_edge, 0.0))
    realized_gross = np.where(side == 1, y-p, np.where(side == -1, p-y, 0.0))
    realized_net = realized_gross - np.where(side != 0, cost, 0.0)
    test["predicted_probability"] = pred
    test["predicted_gross_edge"] = np.where(side == 1, pred-p, np.where(side == -1, p-pred, 0.0))
    test["estimated_cost"] = np.where(side != 0, cost, 0.0)
    test["predicted_net_edge"] = selected_net
    test["side"] = np.where(side == 1, "YES", np.where(side == -1, "NO", "SKIP"))

    rng = np.random.default_rng(SEED)
    mids = test.market_id.astype(str).to_numpy()
    unique = np.unique(mids)
    grouped = {m: np.flatnonzero(mids == m) for m in unique}
    br_diffs, br_skills, expected_net_means, realized_net_means, realized_trade_means = [], [], [], [], []
    for _ in range(BOOTSTRAPS):
        draw = rng.choice(unique, len(unique), replace=True)
        ix = np.concatenate([grouped[m] for m in draw])
        base_brier = np.mean((y[ix]-p[ix])**2)
        model_brier = np.mean((y[ix]-pred[ix])**2)
        br_diffs.append(base_brier - model_brier)
        br_skills.append((base_brier - model_brier) / base_brier)
        expected_net_means.append(float(np.mean(selected_net[ix][entry[ix]])) if entry[ix].any() else 0.0)
        active = side[ix] != 0
        entries = entry[ix]
        realized_net_means.append(float(np.mean(realized_net[ix][entries])) if entries.any() else 0.0)
        realized_trade_means.append(float(np.mean(realized_net[ix][active])) if active.any() else 0.0)

    # Market-volume contradiction: report as descriptive only because lifetime volume
    # is not known at decision time. Stratify on price and lead, then summarize quartiles.
    df["price_bin"] = pd.cut(df.price, [0,.05,.1,.15,.2,.3,.5,.7,.9,1.000001], include_lowest=True)
    df["vol_q"] = pd.qcut(df.volume.astype(float), 4, labels=False, duplicates="drop") + 1
    q1 = df.groupby("vol_q", observed=True).agg(n=("outcome","size"), mean_price=("price","mean"),
         mean_outcome=("outcome","mean"), signed_bias=("calib_err","mean"),
         abs_error=("abs_calib_err","mean")).reset_index()
    strat = df.groupby(["vol_q","price_bin","lead_days"], observed=True).agg(
        n=("outcome","size"), bias=("calib_err","mean")).reset_index()
    # Only compare volume quartiles within price/lead strata with observations in all quartiles.
    counts = strat.groupby(["price_bin","lead_days"], observed=True).vol_q.nunique()
    common = strat.set_index(["price_bin","lead_days"]).index.isin(counts[counts == 4].index)
    balanced = strat[common].copy()
    balanced["stratum"] = balanced["price_bin"].astype(str) + "|" + balanced["lead_days"].astype(str)
    stratum_weights = balanced.groupby("stratum", observed=True).n.sum()
    stratum_weights = stratum_weights / stratum_weights.sum()
    adj = {}
    for q, g in balanced.groupby("vol_q", observed=True):
        g = g.set_index("stratum")
        adj[int(q)] = float((g.bias * stratum_weights.reindex(g.index)).sum())

    # OOS league scan with cluster-robust percentile bootstrap; BH across reported leagues.
    scan_rows = []
    for name, g in test.groupby("league_name", observed=True):
        if g.market_id.nunique() < 20:
            continue
        ix = g.index.to_numpy()
        # Align prediction values by original row index.
        loc = test.index.get_indexer(ix)
        e = y[loc] - p[loc]
        market_ids = g.market_id.astype(str).to_numpy()
        uniq = np.unique(market_ids)
        means = []
        pos = {m: np.flatnonzero(market_ids == m) for m in uniq}
        for _ in range(300):
            draw = rng.choice(uniq, len(uniq), replace=True)
            j = np.concatenate([pos[m] for m in draw])
            means.append(float(e[j].mean()))
        scan_rows.append({"league_name":name,"markets":len(uniq),"rows":len(g),"oos_bias":float(e.mean()),
                          "ci_low":float(np.quantile(means,.025)),"ci_high":float(np.quantile(means,.975)),
                          "p_boot_two_sided":float(min(1,2*min((np.sum(np.asarray(means)<=0)+1)/(len(means)+1),
                                                                  (np.sum(np.asarray(means)>=0)+1)/(len(means)+1))))})
    if scan_rows:
        qvals = bh_adjust([r["p_boot_two_sided"] for r in scan_rows])
        for r,q in zip(scan_rows,qvals): r["p_bh"] = q

    test.to_csv(OUT / "oos_predictions.csv", index=False)
    pd.DataFrame(scan_rows).sort_values("p_bh" if scan_rows else "league_name").to_csv(OUT / "league_oos_scan.csv", index=False)
    report = {
      "data_rows_with_dates":len(df),"markets_with_dates":int(df.market_id.nunique()),
      "markets_missing_date":int(len(ids)-len(end_dates)),"train_markets":int(train.market_id.nunique()),
      "test_markets":int(test.market_id.nunique()),"test_rows":len(test),"cutoff_utc":cutoff.isoformat(),
      "oos_brier_model":brier_model,"oos_brier_market_price":brier_market,
      "oos_brier_skill_vs_market_price":float(1-brier_model/brier_market),"oos_mae_model":mae_model,"oos_mae_market_price":mae_market,
      "brier_diff_cluster_bootstrap_95ci":[float(np.quantile(br_diffs,.025)),float(np.quantile(br_diffs,.975))],
      "brier_skill_cluster_bootstrap_95ci":[float(np.quantile(br_skills,.025)),float(np.quantile(br_skills,.975))],
      "entry_rule":"one decision per market at lead_days == 3",
      "eligible_trade_rows":int(np.sum(side!=0)),"trade_rate_among_3day_rows":float(np.sum(side!=0)/max(np.sum(entry),1)),
      "estimated_cost_mean_when_traded":float(cost[side!=0].mean()) if np.any(side!=0) else None,
      "predicted_gross_edge_mean_when_traded":float(np.mean(np.where(side[side!=0]==1,pred[side!=0]-p[side!=0],p[side!=0]-pred[side!=0]))) if np.any(side!=0) else None,
      "predicted_net_edge_mean_per_trade":float(np.mean(selected_net[side!=0])) if np.any(side!=0) else None,
      "realized_gross_edge_mean_per_trade":float(np.mean(realized_gross[side!=0])) if np.any(side!=0) else None,
      "realized_net_edge_mean_per_trade":float(np.mean(realized_net[side!=0])) if np.any(side!=0) else None,
      "realized_net_edge_cluster_bootstrap_95ci_per_trade":list(map(float,np.quantile(realized_trade_means,[.025,.975]))),
      "realized_net_edge_mean_per_3day_opportunity":float(np.mean(realized_net[entry])),
      "realized_net_edge_cluster_bootstrap_95ci_per_opportunity":list(map(float,np.quantile(realized_net_means,[.025,.975]))),
      "predicted_net_edge_cluster_bootstrap_95ci_per_3day_opportunity":list(map(float,np.quantile(expected_net_means,[.025,.975]))),
      "market_volume_quartiles_descriptive_lifetime_volume":q1.to_dict(orient="records"),
      "volume_bias_after_common_price_lead_strata":{str(k):float(v) for k,v in adj.items()},
      "transaction_cost_assumptions":{"sports_fee_rate":FEE_RATE,"half_spread_per_share":HALF_SPREAD},
      "limitations":["Lifetime volume and observation count excluded from predictive features because they are post-close fields.",
       "The test is chronological by market end date, but this dataset has no archived point-in-time order book; spread cost is an assumed 0.5 cents/share.",
       "This predicts average calibration, not fills or realized portfolio return. Bootstrap clusters by market; same-event cross-market dependence can remain."],
    }
    (OUT / "report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
