"""
alpha_library.py - Alpha 库存库（SQLite + 质量门槛）
====================================================
把挖到的因子按 WorldQuant 等级存进本地数据库，只有达到门槛的才保存，
不达标的直接丢弃（不写入库）。

数据库：alpha_library.db（表 alphas）
命令（经 run.py）：
    python run.py libsave --factor reversal --lookback 30 --min-grade B   # 评分，达标才存
    python run.py liblist                                                 # 查看库
    python run.py librm --id 3                                            # 删除某条
    python run.py libclear                                                # 清空库
"""

import sqlite3
import os
import time

DB = "alpha_library.db"

# 等级顺序（用于比较"是否达到门槛"）
GRADE_LEVEL = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}


def _conn():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS alphas(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, factor TEXT, lookback INTEGER, top_pct REAL, long_only INTEGER,
        grade TEXT, good INTEGER,
        sharpe REAL, fitness REAL, returns REAL, turnover REAL,
        drawdown REAL, margin REAL, conc REAL, ic REAL, icir REAL,
        created TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS candidate_pool(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, factor TEXT, lookback INTEGER, expression TEXT, is_custom INTEGER,
        grade TEXT, good INTEGER, sharpe REAL, fitness REAL,
        returns REAL, turnover REAL, subU REAL, ic REAL,
        created TEXT
    )""")
    return conn


def pool_add(name, factor=None, lookback=None, expression=None, is_custom=False,
             grade=None, good=False, sharpe=None, fitness=None, returns=None,
             turnover=None, subU=None, ic=None):
    conn = _conn()
    cur = conn.execute("""INSERT INTO candidate_pool
        (name, factor, lookback, expression, is_custom, grade, good, sharpe, fitness,
         returns, turnover, subU, ic, created)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, factor, lookback, expression, int(is_custom), grade, int(good),
         sharpe, fitness, returns, turnover, subU, ic,
         time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def pool_list():
    conn = _conn()
    rows = conn.execute("SELECT * FROM candidate_pool ORDER BY id DESC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM candidate_pool").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def pool_rm(pool_id):
    conn = _conn()
    cur = conn.execute("DELETE FROM candidate_pool WHERE id=?", (pool_id,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n > 0


def meets_threshold(grade: str, min_grade: str = "B") -> bool:
    """grade 是否达到 min_grade 门槛。"""
    return GRADE_LEVEL.get(grade, 0) >= GRADE_LEVEL.get(min_grade, 0)


def save(record: dict) -> int:
    """插入一条记录，返回自增 id。"""
    conn = _conn()
    cur = conn.execute("""INSERT INTO alphas
        (name, factor, lookback, top_pct, long_only, grade, good,
         sharpe, fitness, returns, turnover, drawdown, margin, conc, ic, icir, created)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (record.get("name"), record.get("factor"), record.get("lookback"),
         record.get("top_pct"), int(record.get("long_only", True)),
         record.get("grade"), int(record.get("good", False)),
         record.get("sharpe"), record.get("fitness"), record.get("returns"),
         record.get("turnover"), record.get("drawdown"), record.get("margin"),
         record.get("conc"), record.get("ic"), record.get("icir"),
         time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def remove(alpha_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM alphas WHERE id=?", (alpha_id,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n > 0


def clear() -> int:
    conn = _conn()
    cur = conn.execute("DELETE FROM alphas")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def list_alphas() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM alphas ORDER BY id DESC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM alphas").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def print_library() -> None:
    rows = list_alphas()
    if not rows:
        print("(库为空)")
        return
    print(f"Alpha 库共 {len(rows)} 条：")
    print("-" * 92)
    print(f"{'id':>3} {'等级':>4} {'Good':>5} {'Sharpe':>7} {'Fitness':>8} {'年化':>7} {'换手':>6}  因子")
    print("-" * 92)
    for r in rows:
        name = r["name"] or f"{r['factor']}_{r['lookback']}"
        print(f"{r['id']:>3} {r['grade']:>4} {str(bool(r['good'])):>5} "
              f"{r['sharpe']:>7.2f} {r['fitness']:>8.3f} {r['returns']:>6.1%} "
              f"{r['turnover']:>6.1f}  {name}")
    print("-" * 92)
