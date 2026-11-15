# Alpha Miner Framework

A **quality-control loop** for LLM-assisted alpha research on WorldQuant BRAIN.

> 大多数「AI 挖因子」脚本优化的是数量：生成得更多、跑得更多。
> 真正稀缺的能力是**筛掉自己产出的垃圾** —— 判断一条高 Sharpe 的表达式
> 到底是真信号、巧合，还是数学上等价于常数。这个仓库就是那后半段。

```
templates × fields × windows → local validation → platform simulation → gates → report
                                    ↑                    ↑
                          零成本拦截必然失败        唯一花钱的一步
```

---

## Why this exists

Generating candidate expressions is the easy part — a for-loop over templates does it.
The hard part, and the part almost nobody builds, is everything that happens **after** a
number comes back from the platform:

- Is this expression *degenerate*? (`ts_rank(x) - ts_rank(-x)` looks brilliant and means nothing)
- Did it fail because the *hypothesis* is wrong, or because an *operator had no permission*?
- Is it correlated with something already in my portfolio, so that a 1.5 Sharpe is worth nothing?
- Are the 40 candidates I just ran actually 40 ideas, or one idea with 40 sets of parameters?

This repository is the machinery for those four questions. The generator is deliberately
small; the filtering is where the work went.

---

## Modules

| Module | Role |
| --- | --- |
| `brain.py` | API client — auth, retries, rate-limit backoff, self-correlation, SQLite store with schema migration |
| `gen.py` | Template → expression generation, optional LLM generation, syntax + field + operator whitelist |
| `expr_guard.py` | Expression guard: degenerate-construction detection (S1–S4) + operator signature rules (S5–S8), three-state verdict |
| `failure_stats.py` | Failure triage into research / platform-rule / engineering noise, kept strictly separate |
| `selfcorr.py` | Self-correlation pre-check against the submitted pool (`\|ρ\| < 0.7`), plus per-family attribution |
| `corr.py` | Pairwise PnL correlation between candidates — decides *how many can be submitted at once* |
| `sweep.py` | Near-miss repair: a small, fixed set of transformations for candidates just below the gate |
| `blend.py` | Negative-correlation blending: trade diversification for Sharpe |
| `daemon.py` | Long-running loop — resume after crash, per-family quota, operator blacklist with recovery |
| `operators.py` | Operator blacklist CLI (corruption-tolerant) |
| `miner.py` / `run.py` / `pick.py` / `turbo.py` / `watch.py` | CLI entry points and viewers |
| `round_review.py` / `daily_review.py` | Round review / daily report — the LLM sees **blocked facts**, never raw error dumps |
| `push_via_api.py` | Push a commit through the GitHub Data API when `github.com:443` is unreachable |

---

## The four defenses

| Layer | Catches | Cost of skipping it |
| --- | --- | --- |
| `gen.validate` | Misspelled fields, operators the account lacks, syntax errors | Platform returns 400 — simulated budget burned for nothing |
| `expr_guard` | `A - A`, `0 * A`, `A / A`, `ts_rank(x) - ts_rank(-x)`, wrong arity | **Worst case**: a fake high performer you might actually submit |
| `failure_stats` | Conflating "hypothesis rejected" with "operator not permitted" | You tune the wrong thing for a week |
| `selfcorr` / `corr` | Collision with the submitted pool; candidates that are each other's clones | Everything you built is one signal wearing different hats |

### Degenerate constructions are the real enemy

An expression can be perfectly legal, simulate successfully, and produce a
spectacular metric while carrying **zero information**:

| Pattern | Why it's empty |
| --- | --- |
| `close - close` | constant |
| `0 * rank(close)` | constant |
| `debt / debt` | constant |
| `ts_rank(x, 30) - ts_rank(-x, 30)` | for monotone `f`, this is a constant shift of `ts_rank(x)` — redundant, not a signal |

The guard emits **three states**, not two: `STRICT_REJECT` only when the expression
*provably* collapses to a constant; `WARN` when it's redundant but not provably
degenerate; `PASS` otherwise. A filter that rejects everything suspicious becomes a
filter that rejects the signal too.

---

## Three findings worth writing down

### 1. Correlating cumulative PnL is meaningless

The platform's PnL endpoint returns a **cumulative** book value series. Correlating two
of those gives ~0.95 for *anything*, because both are upward-trending curves. My first
implementation did exactly this and confidently reported that every pair of candidates
conflicted with every other pair.

Correlate **daily increments**, not levels:

```python
daily = cumulative.diff().dropna()
rho = np.corrcoef(daily_a.align(daily_b, join="inner")[0],
                  daily_a.align(daily_b, join="inner")[1])[0, 1]
```

After the fix, cross-family correlations turned out to be near zero and often
**negative** — which changed the conclusion from "nothing can be submitted together"
to "these are genuinely diversifying".

### 2. Negative correlation is an asset, not a warning

Two mediocre signals with `ρ < 0` combine into something better than either:

```
S_combo ≈ (S1 + S2) / sqrt(2 + 2ρ)
```

At `ρ = -0.15`, two Sharpe-1.1 signals produce roughly Sharpe 1.7 at *lower* turnover.
`blend.py` implements this: `zscore(signal_a) + zscore(signal_b)`. It is the cheapest
way to move a near-miss across the gate, and it is strictly better than re-running the
same family with a different window.

### 3. Fitness is a turnover trap

```
Fitness = Sharpe × sqrt(|Returns| / max(Turnover, 0.125))
```

A signal can clear Sharpe 1.3 and still fail Fitness because it trades too much. Before
concluding "the signal is weak", check `turnover` — a `decay` sweep frequently fixes it
without touching the hypothesis.

---

## Design principles

**WARN over REJECT.** Only reject what can be *proven* empty. Redundancy is not
degeneracy; annotate it and let a human decide.

**Never auto-submit.** There is no submit code path in this repository, by design.
Deciding *which* alpha is worth submitting is the actual skill being practiced — and
the only thing worth explaining in an interview.

**Classify before concluding.** A failure is a research failure, a platform-rule
failure, or engineering noise. Mixing them produces confident, wrong attributions.

**Hard daily cap.** `DAILY_SIM_CAP` exists so that a bug at 3 a.m. cannot spend a
month of budget. Cheap guards are worth more than clever ones.

**Local-first validation.** Anything decidable locally must be decided locally. Platform
simulation is the only costly resource in the loop.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# fill WQ_EMAIL / WQ_PASS (and optionally a free LLM key)

python miner.py test                                  # verify auth → simulate → fetch → store
python miner.py datasets                              # list datasets
python miner.py fields --dataset fundamental6         # list fields
python miner.py run --mode rules --dry-run --limit 20 # inspect what would run (no budget)
python miner.py run --mode rules --limit 60           # for real
python analyze.py                                     # prune + report
```

Then, once you have results in the local database:

```bash
python selfcorr.py            # which candidates survive the 0.7 self-correlation gate
python selfcorr.py --all      # attribute collisions per template family
python corr.py <id1> <id2>    # can these be submitted together? (daily-increment rho)
python sweep.py --dry-run     # near-miss repair plan
python blend.py --dry-run     # negative-correlation blends (edit ANCHOR/PARTNERS first)
```

> **Before using `blend.py`**: replace `ANCHOR` and `PARTNERS` with your own expressions.
> The placeholders are neutral examples, not recommendations.

---

## What is deliberately absent

- **No alpha expressions and no performance numbers.** The research output — which
  fields, which families, which parameters actually worked — is not in this repository.
  That is the part a researcher keeps. What is here is the method.
- **No credentials.** Everything is read from `.env`, which is git-ignored.
- **No account data.** `data/` (SQLite database, logs) is excluded.

## Compliance boundaries

The tool is intentionally narrow: it generates candidates, calls the official API to
simulate them, stores results locally, and analyses them. It does **not** submit alphas,
rotate accounts, scrape other users' data, or attempt to bypass any platform check.
Quota caps and a "no submit path" rule are structural, not configuration.

---

## 中文说明

这个仓库是我做基本面 alpha 研究时写的**筛选与归因层**：生成器很小，绝大部分
代码用于在回测前后判断「这条表达式值不值得看」。

三个我认为最有价值的判断：

1. **三重本地校验** —— 语法/字段/算子权限、退化构造、签名规则，全部在本地零成本拦掉，
   不让平台额度浪费在必然失败的请求上。
2. **失败三分法** —— 研究失败、平台规则失败、工程噪声必须分开统计，否则归因一定是错的。
3. **两种相关性** —— 候选与已提交池的自相关（决定能不能交），候选之间的互相关
   （决定这一批能同时交几条）。而且必须对**每日增量**求相关，对累计 PnL 求出来的
   相关全是 0.95，纯属自欺。

方法层面的取舍写在上面各节里。具体的因子与绩效不在这里 —— 那属于研究资产。

---

## License

MIT
