"""polymarket.config - 模块统一配置

数据目录解析顺序:
    1. 环境变量 PM_DATA_DIR
    2. 模块内 data/ （默认）
    3. 回退到旧的 polymarket_sports/data/（如果你之前已采过数据）

这样模块可以自包含，也能复用已有的大数据集。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# 旧数据位置（之前采集的约 1GB 数据）
LEGACY = r"D:\26050\Documents\polymarket_sports\data"


def data_dir() -> str:
    env = os.environ.get("PM_DATA_DIR")
    if env:
        return env
    local = os.path.join(HERE, "data")
    if os.path.isdir(local) and os.listdir(local):
        return local
    if os.path.isdir(LEGACY) and os.listdir(LEGACY):
        return LEGACY
    return local


def paths():
    d = data_dir()
    return {
        "data": d,
        "markets": os.path.join(d, "markets"),
        "prices": os.path.join(d, "prices"),
        "catalog": os.path.join(d, "sports_catalog.json"),
        "dataset_all": os.path.join(d, "dataset_all.csv"),
        "dataset_tradable": os.path.join(d, "dataset_tradable.csv"),
    }


# API 端点
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
