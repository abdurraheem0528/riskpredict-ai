"""Spike detection on stored series: robust z-score, plus IsolationForest.

For every observation in a key's recent evaluation range:

1. **Robust z-score.** The trailing window ``[t - window, t)`` gives a median
   and a spread of ``1.4826 × MAD``. If the MAD is 0 (a mostly flat window),
   ``1.2533 × mean absolute deviation`` is used instead (Iglewicz & Hoaglin).
   For count series the spread is never below ``sqrt(max(median, 1))``,
   roughly the Poisson noise level, so 0 → 3 articles is not a "spike". A
   point is flagged when ``|z| ≥ threshold`` (only ``z ≥ threshold`` for
   one-sided rules). If the window has fewer than ``min_points`` observations,
   nothing is flagged and the key is reported as ``insufficient_history``.
2. **IsolationForest (second opinion).** If the window before the evaluation
   range holds at least ``if_min_points`` observations, a forest is fitted on
   them (features: value and change from the previous value) and the
   evaluation points are scored. A point is flagged when it is more isolated
   than the training data's ``IF_CONTAMINATION`` quantile. Both methods are
   stored as separate rows, so agreement can be checked.

Exchange rates trend, so they are scored on daily log returns; their stored
``baseline`` is then the previous rate moved by the window's median return,
which keeps ``value`` and ``baseline`` in the same unit. Other series are
scored on their levels.

Nothing here reads a database: :func:`detect` takes plain ``(ts, value)``
lists, so it runs the same on stored history and in dry runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Sequence

import numpy as np

MAD_TO_SD = 1.4826
MEAN_AD_TO_SD = 1.2533
DEFAULT_THRESHOLD = 3.5
IF_CONTAMINATION = 0.02
IF_TREES = 100
NEWS_SERIES = "gdelt.articles"


@dataclass(frozen=True)
class Rule:
    name: str
    prefixes: tuple[str, ...]
    window_days: int = 90
    min_points: int = 30
    if_min_points: int = 60
    transform: Literal["level", "log_return"] = "level"
    two_sided: bool = True
    counts: bool = False
    threshold: float = DEFAULT_THRESHOLD
    eval_days: int = 14
    skip_reason: str | None = None

    def matches(self, series: str) -> bool:
        return any(series == p or (p.endswith(".") and series.startswith(p)) for p in self.prefixes)


RULES: tuple[Rule, ...] = (
    Rule("news_volume", (NEWS_SERIES,), window_days=30, min_points=14, if_min_points=25, two_sided=False, counts=True),
    Rule("fx", ("fx.rate",), window_days=90, min_points=40, if_min_points=55, transform="log_return"),
    Rule("port_calls", ("portwatch.port_calls",), window_days=90, min_points=30, if_min_points=60, counts=True),
    Rule("trade_volume", ("portwatch.import_volume", "portwatch.export_volume"), window_days=90, min_points=30),
    # Annual indicators: a 10-year window needs 8 values; the API gives 5, so these
    # report insufficient history until more years accumulate.
    Rule("macro_annual", ("wb.",), window_days=3660, min_points=8, if_min_points=12, eval_days=400),
    Rule(
        "epss",
        ("epss.score",),
        skip_reason="scores are only stored when >= EPSS_MIN_SCORE, so the history is censored",
    ),
)
DEFAULT_RULE = Rule("other", ())


def rule_for(series: str) -> Rule:
    return next((r for r in RULES if r.matches(series)), DEFAULT_RULE)


@dataclass(frozen=True)
class Flag:
    series: str
    key: str
    ts: datetime
    method: str  # 'robust_z' | 'isolation_forest'
    value: float
    baseline: float
    score: float
    attrs: dict


@dataclass
class Detection:
    series: str
    key: str
    rule: Rule
    status: str  # 'ok' | 'insufficient_history' | 'skipped' | 'empty'
    history_points: int = 0
    needed_points: int = 0
    flags: list[Flag] = field(default_factory=list)
    eval_from: datetime | None = None
    notes: dict[str, int] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return f"{self.series}|{self.key}"


def robust_scale(window: np.ndarray, counts: bool = False) -> tuple[float, float]:
    """``(median, spread)`` of a window; spread 0 means it cannot be scored."""
    med = float(np.median(window))
    dev = np.abs(window - med)
    scale = MAD_TO_SD * float(np.median(dev))
    if scale == 0.0:
        scale = MEAN_AD_TO_SD * float(np.mean(dev))
    if counts:
        scale = max(scale, math.sqrt(max(med, 1.0)))
    return med, scale


def robust_z(window: Sequence[float], x: float, counts: bool = False) -> tuple[float, float, float | None]:
    """``(median, spread, z)``; ``z`` is ``None`` when the spread is 0."""
    med, scale = robust_scale(np.asarray(window, dtype=float), counts)
    return med, scale, ((x - med) / scale if scale > 0 else None)


def _observations(points: Sequence[tuple[datetime, float]], transform: str):
    """Sorted, de-duplicated ``(ts, y, value, prev_value)`` rows."""
    by_ts: dict[datetime, float] = {}
    for ts, v in points:
        if v is not None and math.isfinite(v):
            by_ts[ts] = float(v)
    rows = sorted(by_ts.items())
    if transform == "level":
        return [(ts, v, v, rows[i - 1][1] if i else None) for i, (ts, v) in enumerate(rows)]
    out = []
    for (_, prev), (ts, v) in zip(rows, rows[1:]):
        if prev > 0 and v > 0:
            out.append((ts, math.log(v / prev), v, prev))
    return out


def _baseline(rule: Rule, center: float, prev_value: float | None) -> float:
    if rule.transform == "log_return":
        return prev_value * math.exp(center) if prev_value is not None else float("nan")
    return center


def detect(series: str, key: str, points: Sequence[tuple[datetime, float]], rule: Rule | None = None) -> Detection:
    rule = rule or rule_for(series)
    det = Detection(series, key, rule, "empty", needed_points=rule.min_points)
    if rule.skip_reason:
        det.status = "skipped"
        return det
    obs = _observations(points, rule.transform)
    if not obs:
        return det
    window = timedelta(days=rule.window_days)
    latest = obs[-1][0]
    eval_from = latest - timedelta(days=rule.eval_days)
    det.eval_from = eval_from
    ts_arr = np.array([o[0].timestamp() for o in obs])
    y_arr = np.array([o[1] for o in obs])
    common = {"rule": rule.name, "window_days": rule.window_days, "transform": rule.transform}

    scored = 0
    for i, (ts, y, value, prev) in enumerate(obs):
        if ts <= eval_from:
            continue
        lo = np.searchsorted(ts_arr, (ts - window).timestamp(), side="left")
        hist = y_arr[lo:i]
        det.history_points = max(det.history_points, len(hist))
        if len(hist) < rule.min_points:
            det.notes["insufficient"] = det.notes.get("insufficient", 0) + 1
            continue
        med, _, z = robust_z(hist, y, rule.counts)
        if z is None:
            det.notes["zero_spread"] = det.notes.get("zero_spread", 0) + 1
            continue
        scored += 1
        if z >= rule.threshold or (rule.two_sided and z <= -rule.threshold):
            det.flags.append(
                Flag(
                    series, key, ts, "robust_z", value, _baseline(rule, med, prev), round(z, 4),
                    {**common, "points": int(len(hist)), "threshold": rule.threshold},
                )
            )
    det.notes["scored"] = scored
    det.status = "ok" if scored or det.notes.get("zero_spread") else "insufficient_history"
    if det.status == "ok":
        det.flags.extend(_isolation_forest(series, key, obs, eval_from, rule, common))
    return det


def _features(obs, idx: Sequence[int]) -> np.ndarray:
    rows = []
    for i in idx:
        y = obs[i][1]
        rows.append((y, y - obs[i - 1][1] if i else 0.0))
    return np.array(rows, dtype=float)


def _isolation_forest(series, key, obs, eval_from: datetime, rule: Rule, common: dict) -> list[Flag]:
    from sklearn.ensemble import IsolationForest

    first_eval = next(i for i, o in enumerate(obs) if o[0] > eval_from)
    train_from = obs[first_eval][0] - timedelta(days=rule.window_days)
    train_idx = [i for i in range(1, first_eval) if obs[i][0] >= train_from]  # i >= 1: needs a previous value
    if len(train_idx) < rule.if_min_points:
        return []
    eval_idx = list(range(first_eval, len(obs)))
    train = _features(obs, train_idx)
    forest = IsolationForest(
        n_estimators=IF_TREES, contamination=IF_CONTAMINATION, random_state=0, max_samples=min(256, len(train))
    ).fit(train)
    test = _features(obs, eval_idx)
    pred = forest.predict(test)
    scores = -forest.score_samples(test)  # 0..1, higher = more isolated
    center = float(np.median(train[:, 0]))
    flags = []
    for j, i in enumerate(eval_idx):
        ts, y, value, prev = obs[i]
        if pred[j] != -1 or (not rule.two_sided and y <= center):
            continue
        flags.append(
            Flag(
                series, key, ts, "isolation_forest", value, _baseline(rule, center, prev), round(float(scores[j]), 4),
                {**common, "points": len(train_idx), "contamination": IF_CONTAMINATION},
            )
        )
    return flags


def daily_counts(
    rows: Sequence[tuple[str, datetime, int]], terms: Sequence[str]
) -> dict[str, list[tuple[datetime, float]]]:
    """Daily article counts per watch term from ``(term, day, count)`` rows.

    A day on which *no* term has any stored article is treated as missing (most
    likely a fetch outage or throttling), not as a zero for every term. On days
    with data, terms without articles count 0.
    """
    days = sorted({d for _, d, n in rows if n > 0})
    by: dict[tuple[str, datetime], int] = {}
    for term, day, n in rows:
        by[(term, day)] = by.get((term, day), 0) + int(n)
    out: dict[str, list[tuple[datetime, float]]] = {}
    for term in dict.fromkeys([*terms, *(t for t, _, _ in rows)]):
        out[term] = [(d, float(by.get((term, d), 0))) for d in days]
    return out
