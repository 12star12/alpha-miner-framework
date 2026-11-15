# -*- coding: utf-8 -*-
"""
BRAIN 平台客户端 + 本地落库。

设计原则：
1. 所有网络失败都重试有限次，之后返回 (None, 原因)，绝不抛异常中断整夜任务。
2. 每条模拟结果立刻写 SQLite —— 跑到一半崩了，重跑不会重复烧额度。
3. 限流（429）按 Retry-After 退避，不硬闯。
"""
import base64
import json
import sqlite3
import threading
import time
import hashlib
import traceback
from datetime import datetime
from pathlib import Path

import requests

import config


class BrainError(Exception):
    pass


class AuthError(BrainError):
    pass


# ---------------------------------------------------------------- 凭证加载
def load_env(path: Path = None) -> None:
    """极简 .env 读取，不引入 python-dotenv。已有环境变量优先。"""
    path = path or (config.ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not config.os.getenv(k):
            config.os.environ[k] = v


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 客户端
class BrainClient:
    def __init__(self, email: str = None, password: str = None):
        self.email = email or config.WQ_EMAIL
        self.password = password or config.WQ_PASS
        if not self.email or not self.password:
            raise AuthError("缺少凭证：请在项目根目录的 .env 里填 WQ_EMAIL / WQ_PASS")
        self.base = config.WQ_BASE.rstrip("/")
        self.s = requests.Session()
        token = base64.b64encode(f"{self.email}:{self.password}".encode()).decode()
        self.s.headers.update({
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "alpha-miner/1.0",
        })
        self._authenticate()

    # ------------------------------------------------------------ 认证
    def _authenticate(self) -> None:
        try:
            r = self.s.post(f"{self.base}/authentication", timeout=30)
        except requests.RequestException as e:
            raise AuthError(f"连不上平台：{e}")
        if r.status_code in (200, 201):
            log("认证成功")
            return
        if r.status_code == 401:
            raise AuthError("账号或密码错误（401）")
        if r.status_code == 403:
            raise AuthError(
                "需要验证身份（403）。解决办法：先用浏览器登录 platform.worldquantbrain.com 完成 "
                "Persona 验证，再回来跑脚本，通常同机同日就不再拦。"
            )
        raise AuthError(f"认证失败 {r.status_code}: {r.text[:200]}")

    def _get(self, url: str, **kw) -> requests.Response:
        """带 429 退避的 GET。"""
        for attempt in range(config.MAX_RETRY_429):
            r = self.s.get(url, timeout=60, **kw)
            if r.status_code != 429:
                return r
            wait = float(r.headers.get("Retry-After", 5))
            log(f"限流 429，等待 {wait:.0f}s 后重试")
            time.sleep(wait)
        return r

    # ------------------------------------------------------------ 数据集 / 字段
    def list_datasets(self, settings: dict = None) -> list:
        st = settings or config.SIM_SETTINGS
        url = (
            f"{self.base}/data-sets?instrumentType={st['instrumentType']}"
            f"&region={st['region']}&delay={st['delay']}&universe={st['universe']}&limit=50"
        )
        out, offset = [], 0
        while True:
            r = self._get(f"{url}&offset={offset}")
            if r.status_code != 200:
                log(f"拉数据集失败 {r.status_code}: {r.text[:200]}")
                break
            d = r.json()
            batch = d.get("results", [])
            out.extend(batch)
            if len(out) >= d.get("count", 0) or not batch:
                break
            offset += 50
        return out

    def list_fields(self, dataset_id: str, settings: dict = None) -> list:
        """拉某个数据集下的全部字段（只保留可用于表达式的 MATRIX 类型）。"""
        st = settings or config.SIM_SETTINGS
        url = (
            f"{self.base}/data-fields?instrumentType={st['instrumentType']}"
            f"&region={st['region']}&delay={st['delay']}&universe={st['universe']}"
            f"&dataset.id={dataset_id}&limit=50"
        )
        out, offset = [], 0
        while True:
            r = self._get(f"{url}&offset={offset}")
            if r.status_code != 200:
                log(f"拉字段失败 {r.status_code}: {r.text[:200]}")
                break
            d = r.json()
            batch = d.get("results", [])
            for f in batch:
                if f.get("type") in ("MATRIX", "MATRIX_2D"):
                    out.append({
                        "id": f.get("id"),
                        "description": (f.get("description") or "")[:120],
                        "dataset": dataset_id,
                        "coverage": f.get("coverage"),
                        "userCount": f.get("userCount"),
                    })
            if len(out) >= d.get("count", 0) or not batch:
                break
            offset += 50
        return out

    # ------------------------------------------------------------ 模拟
    def simulate(self, expression: str, settings: dict = None) -> dict:
        """
        跑一次回测。返回 {'alpha_id':..., 'metrics':{...}, 'raw':{...}}
        失败返回 {'error': '原因'}
        """
        st = dict(settings or config.SIM_SETTINGS)
        payload = {"type": "REGULAR", "settings": st, "regular": expression}

        for attempt in range(config.MAX_RETRY_429):
            try:
                r = self.s.post(f"{self.base}/simulations", json=payload, timeout=60)
            except requests.RequestException as e:
                return {"error": f"网络异常: {e}"}

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 5))
                time.sleep(wait)
                continue
            if r.status_code in (400, 401, 403, 422):
                # 400/422 多半是表达式语法或字段不合法，直接放弃这条，别重试
                return {"error": f"HTTP {r.status_code}: {r.text[:180]}"}
            if r.status_code != 201:
                return {"error": f"HTTP {r.status_code}: {r.text[:180]}"}
            break
        else:
            return {"error": "多次限流，放弃"}

        progress_url = r.headers.get("Location")
        if not progress_url:
            return {"error": "没拿到 Location，无法追踪进度"}

        return self._poll(progress_url)

    def _poll(self, progress_url: str) -> dict:
        t0, last = time.time(), None
        while time.time() - t0 < config.POLL_TIMEOUT:
            r = self._get(progress_url)
            if r.status_code == 429:
                time.sleep(2)
                continue
            if r.status_code != 200:
                return {"error": f"轮询失败 HTTP {r.status_code}"}

            try:
                d = r.json()
            except ValueError:
                time.sleep(config.POLL_INTERVAL)
                continue

            status = (d.get("status") or "").upper()
            last = d
            if status in ("COMPLETE", "DONE"):
                alpha_id = d.get("alpha") or d.get("alphaId")
                if not alpha_id:
                    # 有些版本完成体只给 simulation id，再查一次
                    sim_id = progress_url.rstrip("/").split("/")[-1]
                    rr = self._get(f"{self.base}/simulations/{sim_id}")
                    if rr.status_code == 200:
                        alpha_id = rr.json().get("alpha")
                if not alpha_id:
                    return {"error": "模拟完成但拿不到 alpha id"}
                return self._fetch_alpha(alpha_id, expression=None)

            if status in ("ERROR", "FAIL", "FAILED", "CANCELLED"):
                msg = d.get("message") or d.get("error") or status
                return {"error": f"模拟失败: {str(msg)[:180]}"}

            time.sleep(config.POLL_INTERVAL)
        return {"error": "模拟超时"}

    def _fetch_alpha(self, alpha_id: str, expression: str = None) -> dict:
        r = self._get(f"{self.base}/alphas/{alpha_id}")
        if r.status_code != 200:
            return {"error": f"取结果失败 HTTP {r.status_code}"}
        d = r.json()
        is_ = d.get("is", {}) or {}
        metrics = {
            "sharpe": is_.get("sharpe"),
            "fitness": is_.get("fitness"),
            "turnover": is_.get("turnover"),
            "returns": is_.get("returns"),
            "drawdown": is_.get("drawdown"),
            "margin": is_.get("margin"),
            "longCount": is_.get("longCount"),
            "shortCount": is_.get("shortCount"),
        }
        checks = is_.get("checks", []) or []
        failed = [c.get("name") for c in checks if str(c.get("result")).upper() == "FAIL"]
        return {
            "alpha_id": alpha_id,
            "expression": expression or d.get("regular", {}).get("code") or d.get("regular"),
            "metrics": metrics,
            "failed_checks": failed,
            "checks": checks,
        }

    # ------------------------------------------------------------ 自相关
    def self_correlation(self, alpha_id: str, tries: int = 3) -> dict:
        """
        查一条 alpha 与自己已提交池的最大自相关（平台提交检查口径，cutoff 0.7）。
        返回 {'max': 带符号最大值, 'max_abs': 绝对值最大值, 'error': None 或原因}。
        平台算这个值可能要几秒，查不到就重试几次，仍不行如实返回 error，绝不打断主流程。
        """
        url = f"{self.base}/alphas/{alpha_id}/correlations/self"
        for i in range(tries):
            r = self._get(url)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 3)))
                continue
            if r.status_code != 200:
                return {"max": None, "max_abs": None, "error": f"HTTP {r.status_code}"}
            try:
                d = r.json()
            except ValueError:
                time.sleep(3)
                continue
            parsed = parse_selfcorr(d)
            if parsed["max_abs"] is not None:
                parsed["error"] = None
                return parsed
            if i < tries - 1:
                time.sleep(4)      # 平台大概率还在算，稍等再查
        return {"max": None, "max_abs": None, "error": "平台未返回数据（可能还在计算）"}


def parse_selfcorr(d: dict) -> dict:
    """
    解析 /correlations/self 响应。这个接口返回的不是一个数字，是一张明细表：
    schema.properties 定义列名，records 每行是一条与候选相关的已提交 alpha，
    列含 id / correlation / sharpe 等。
    返回 {'max', 'max_abs', 'matched_id', 'matched_sharpe', 'detail'}；
    解不出就返回 None 值，让调用方如实记录「查不到」，绝不猜数。
    """
    props = ((d.get("schema") or {}).get("properties")) or []
    cols = [p.get("name") for p in props]
    if "correlation" not in cols:
        return {"max": None, "max_abs": None, "matched_id": None,
                "matched_sharpe": None, "detail": []}
    i_corr = cols.index("correlation")

    detail = []
    for row in d.get("records") or []:
        if not isinstance(row, (list, tuple)) or len(row) <= i_corr:
            continue
        v = row[i_corr]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        item = {"correlation": v}
        for key in ("id", "sharpe"):
            if key in cols:
                item[key] = row[cols.index(key)]
        detail.append(item)

    if not detail:
        return {"max": None, "max_abs": None, "matched_id": None,
                "matched_sharpe": None, "detail": []}
    top = max(detail, key=lambda x: abs(x["correlation"]))
    return {
        "max": max(x["correlation"] for x in detail),
        "max_abs": abs(top["correlation"]),
        "matched_id": top.get("id"),
        "matched_sharpe": top.get("sharpe"),
        "detail": detail[:5],
    }


# ---------------------------------------------------------------- 本地库
SCHEMA = """
CREATE TABLE IF NOT EXISTS alphas (
    alpha_id     TEXT PRIMARY KEY,
    expression   TEXT NOT NULL,
    template     TEXT,
    field        TEXT,
    expr_hash    TEXT,
    sharpe       REAL,
    fitness      REAL,
    turnover     REAL,
    returns      REAL,
    drawdown     REAL,
    margin       REAL,
    failed_checks TEXT,
    source       TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hash ON alphas(expr_hash);
CREATE INDEX IF NOT EXISTS idx_sharpe ON alphas(sharpe);

CREATE TABLE IF NOT EXISTS attempts (
    expr_hash  TEXT,
    expression TEXT,
    template   TEXT,
    field      TEXT,
    ok         INTEGER,
    error      TEXT,
    seconds    REAL,
    created_at TEXT
);

-- 自相关预检旁表：只追加，不动 alphas / attempts 现有表和历史记录。
CREATE TABLE IF NOT EXISTS selfcorr (
    alpha_id       TEXT PRIMARY KEY,
    max_corr       REAL,
    max_abs        REAL,
    matched_id     TEXT,
    matched_sharpe REAL,
    error          TEXT,
    checked_at     TEXT
);
"""


def settings_key(settings: dict) -> str:
    """参数指纹：同一表达式换了 decay/neutralization，就是一条全新的记录。"""
    return hashlib.md5(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:8]


class Store:
    def __init__(self, path: Path = None):
        self.path = str(path or config.DB_PATH)
        # 多线程共用一条连接：必须关掉线程检查，再用锁串行化写入
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：老库里已存在的表缺新列时自动补上（幂等，只加列不改数据）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(selfcorr)")}
        if not cols:
            return
        for name, decl in (("matched_id", "TEXT"), ("matched_sharpe", "REAL")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE selfcorr ADD COLUMN {name} {decl}")
                log(f"selfcorr 表补列：{name}")

    @staticmethod
    def _hash(expr: str, settings_key: str = None) -> str:
        """带上参数指纹：改了 decay / neutralization，同一条表达式算新记录。"""
        key = settings_key if settings_key is not None else config.SETTINGS_KEY
        return hashlib.md5((key + "|" + expr.strip()).encode("utf-8")).hexdigest()

    def seen(self, expr: str, settings_key: str = None) -> bool:
        """这个表达式跑过或跑挂过，都算见过，避免重复烧额度。"""
        h = self._hash(expr, settings_key)
        with self._lock:
            cur = self.conn.execute("SELECT 1 FROM attempts WHERE expr_hash=? LIMIT 1", (h,))
            return cur.fetchone() is not None

    def seen_real(self, expr: str, settings_key: str = None) -> bool:
        """只把「真跑过」算作见过：限流/网络/超时属于工程噪声，不算，允许重试。

        区别很关键：真失败（语法/字段/单位）重跑也是失败，别浪费额度；
        限流只是平台忙，等一会儿就能跑，不能因此永久跳过。
        """
        h = self._hash(expr, settings_key)
        with self._lock:
            rows = self.conn.execute(
                "SELECT ok, error FROM attempts WHERE expr_hash=?", (h,)
            ).fetchall()
        if not rows:
            return False
        noise = ("限流", "网络", "超时", "429")
        for ok, err in rows:
            if ok:
                return True
            if not any(p in (err or "") for p in noise):
                return True
        return False

    def save_attempt(self, expr, template, field, ok, error=None, seconds=0.0,
                     settings_key: str = None):
        """写库失败绝不能打断主流程——回测已经花掉了，结果必须留下。"""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO attempts(expr_hash, expression, template, field, ok, error, seconds, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (self._hash(expr, settings_key), expr, template, field, 1 if ok else 0,
                     (error or "")[:300], seconds, datetime.now().isoformat(timespec="seconds")),
                )
                self.conn.commit()
        except Exception as e:
            log(f"写 attempts 失败（不影响继续）：{e}")

    def save_alpha(self, res: dict, template=None, field=None, source="rules",
                   settings_key: str = None):
        m = res.get("metrics", {})
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO alphas(alpha_id, expression, template, field, expr_hash,"
                    " sharpe, fitness, turnover, returns, drawdown, margin, failed_checks, source, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (res["alpha_id"], res.get("expression") or "", template, field,
                     self._hash(res.get("expression") or "", settings_key),
                     m.get("sharpe"), m.get("fitness"), m.get("turnover"),
                     m.get("returns"), m.get("drawdown"), m.get("margin"),
                     json.dumps(res.get("failed_checks", []), ensure_ascii=False),
                     source, datetime.now().isoformat(timespec="seconds")),
                )
                self.conn.commit()
        except Exception as e:
            log(f"写 alphas 失败（结果丢失）：{e}")

    def save_selfcorr(self, alpha_id: str, sc: dict) -> None:
        """自相关预检结果落旁表。写失败不影响主流程。"""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO selfcorr(alpha_id, max_corr, max_abs,"
                    " matched_id, matched_sharpe, error, checked_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (alpha_id, sc.get("max"), sc.get("max_abs"),
                     sc.get("matched_id"), sc.get("matched_sharpe"),
                     sc.get("error"), datetime.now().isoformat(timespec="seconds")),
                )
                self.conn.commit()
        except Exception as e:
            log(f"写 selfcorr 失败（不影响继续）：{e}")

    def stats(self) -> dict:
        with self._lock:
            q = self.conn.execute
            total = q("SELECT COUNT(*) FROM alphas").fetchone()[0]
            att = q("SELECT COUNT(*) FROM attempts").fetchone()[0]
            ok = q("SELECT COUNT(*) FROM attempts WHERE ok=1").fetchone()[0]
            best = q("SELECT MAX(sharpe) FROM alphas").fetchone()[0]
        return {"alphas": total, "attempts": att, "success": ok, "best_sharpe": best}
