# -*- coding: utf-8 -*-
"""
全局配置。所有可调参数集中在这里，改这一个文件就够了。

注意：不要把邮箱密码写进本文件。凭证放 .env，代码从环境变量读。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """在读取任何环境变量之前，先把同目录 .env 灌进 os.environ。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v


_load_dotenv()

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "alphas.db"
FIELDS_CACHE = DATA_DIR / "fields.json"
REPORT_PATH = DATA_DIR / "report.md"
LOG_PATH = DATA_DIR / "run.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 凭证
WQ_BASE = "https://api.worldquantbrain.com"
WQ_EMAIL = os.getenv("WQ_EMAIL", "").strip()
WQ_PASS = os.getenv("WQ_PASS", "").strip()

# 免费 LLM（OpenAI 兼容）。默认智谱 GLM-4-Flash：永久免费、30 并发、国内直连。
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flash").strip()

# ---------------------------------------------------------------- 模拟设置
# 第一阶段固定 USA / TOP3000 / delay1：字段最全、文档最全、社区案例最多，出错最少。
SIM_SETTINGS = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 20,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": False,
}

# ---------------------------------------------------------------- 运行控制
MAX_WORKERS = 3          # 平台并发上限 3-5，默认账户按 3 跑，别贪
SLEEP_BETWEEN = 0.6      # 每次提交之间的间隔（秒）
POLL_INTERVAL = 2.5      # 轮询模拟进度的间隔（秒）
POLL_TIMEOUT = 600       # 单次模拟最长等待（秒）
MAX_RETRY_429 = 5        # 遇到限流的重试次数

# ---------------------------------------------------------------- 合规护栏
# 这几个值不是性能参数，是自保参数。
# 平台明令禁止 noise injection / gaming；用垃圾表达式堆量换津贴，属于可被终止账号的行为。
# 本工具的设计边界：只做「回测 + 落库 + 本地分析」，不提供任何自动提交功能。
DAILY_SIM_CAP = 1200     # 每日模拟硬上限，超过当天直接罢工（不是平台限制，是自律线）
MIN_VALID_RATE = 0.30    # 若有效生成率低于此值，说明生成器在产垃圾，强制停手检查

# ---------------------------------------------------------------- 字段选择
# 黑名单：这些是「工具字段」，用来复权、拆股、算市值的，本身不是信号。
# 拿它们去做因子，跑得出数字，但没有经济含义 —— 属于自欺欺人。
FIELD_BLACKLIST = {
    "adjfactor", "split", "split_adj_factor", "dividend",
    "sharesout", "shares_out", "universe", "ticker", "sedol", "cusip",
}

# 优先顺序：有明确经济含义的核心价量字段排前面
FIELD_PRIORITY = [
    "close", "open", "high", "low", "vwap", "volume", "returns", "cap", "adv20",
]

# 白名单（非空时只跑这些字段，忽略 FIELD_PRIORITY 的"其余字段也算"逻辑）
#
# 为什么需要它：字段数 × 模板数 × 窗口数是乘出来的。放开全字段，额度很快就
# 摊薄在一堆没人验证过的字段上。先用一小撮「有明确经济含义」的字段把方法论跑通，
# 再按需放量。
# 想放开全部字段，把它改成 [] 即可。
FIELD_ONLY = [
    "close",
    "assets", "sales", "capex", "ebit", "cashflow_op",
    "debt", "eps", "equity", "cogs",
]

# ---------------------------------------------------------------- 筛选门槛
# 来源：平台提交检查的公开口径（会变，以平台为准）
FILTER = {
    "sharpe_min": 1.25,
    "fitness_min": 1.0,
    "turnover_min": 0.01,
    "turnover_max": 0.70,
    # 与自己已提交池的最大自相关上限。平台 cutoff 就是 0.7（Sharpe 高出 10% 可豁免），
    # 这里是提交前的本地预检：候选过线但自相关 >= 0.7 的，交了也会被拒。
    "self_corr_max": 0.70,
}
# 内部初筛可以放宽，先看分布再收紧
FILTER_LOOSE = {
    "sharpe_min": 1.00,
    "fitness_min": 0.80,
    "turnover_min": 0.01,
    "turnover_max": 0.90,
}

# 参数指纹：模拟设置变了（比如 decay 从 4 调到 20），同样的表达式算全新的一条，
# 否则会被当成「跑过了」直接跳过，你会以为参数没起作用。
import hashlib as _hashlib
import json as _json

SETTINGS_KEY = _hashlib.md5(
    _json.dumps(SIM_SETTINGS, sort_keys=True).encode()
).hexdigest()[:8]
