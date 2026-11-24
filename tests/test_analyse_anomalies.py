"""Spike detection on real series values.

``frankfurter_history.json`` is a real ECB reference-rate response covering
146 business days; step 1's fixtures supply real short series (too short to
score, on purpose). Where a spike is needed, one real value is multiplied in
the test, and every expected z-score is recomputed here with plain numpy.
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from conftest import fixture_json

from jobs.analyse import anomalies
from jobs.analyse.anomalies import detect, robust_z, rule_for
from jobs.connectors import frankfurter, portwatch, worldbank


def fx_history():
    out = {}
    for p in frankfurter.parse(fixture_json("frankfurter_history.json")):
        out.setdefault(p.key, []).append((p.ts, p.value))
    return {k: sorted(v) for k, v in out.items()}


FX = fx_history()


def day(n):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)


# --- the statistics themselves -------------------------------------------------


def test_robust_z_uses_median_and_mad():
    med, scale, z = robust_z([1, 2, 3, 4, 100], 10)
    assert med == 3 and scale == pytest.approx(1.4826)
    assert z == pytest.approx(7 / 1.4826)


def test_zero_mad_falls_back_to_mean_absolute_deviation():
    med, scale, z = robust_z([5, 5, 5, 5, 9], 9)
    assert med == 5 and scale == pytest.approx(1.2533 * 0.8)
    assert z == pytest.approx(4 / (1.2533 * 0.8))


def test_flat_window_cannot_be_scored():
    assert robust_z([7.0] * 30, 8.0) == (7.0, 0.0, None)


def test_count_series_spread_has_a_poisson_floor():
    _, scale, z = robust_z([0] * 20, 3, counts=True)
    assert scale == 1.0 and z == 3.0  # 0 -> 3 articles is below the 3.5 threshold
    _, scale, _ = robust_z([100, 100, 101, 99] * 5, 130, counts=True)
    assert scale == pytest.approx(10.0)  # sqrt(100) beats 1.4826 * MAD


def test_rules():
    assert rule_for("fx.rate").name == "fx" and rule_for("fx.rate").transform == "log_return"
    assert rule_for("fx.rate").window_days == 90
    assert rule_for("portwatch.port_calls").window_days == 90
    assert rule_for("portwatch.export_volume").name == "trade_volume"
    assert rule_for(anomalies.NEWS_SERIES).window_days == 30 and not rule_for(anomalies.NEWS_SERIES).two_sided
    assert rule_for("wb.FP.CPI.TOTL.ZG").name == "macro_annual"
    assert rule_for("epss.score").skip_reason
    assert rule_for("something.new") is anomalies.DEFAULT_RULE


# --- real exchange rates --------------------------------------------------------


def expected_z(points, i, window_days=90):
    """Independent robust z of the i-th log return over the trailing window."""
    ts = [t for t, _ in points]
    r = [math.log(points[k][1] / points[k - 1][1]) for k in range(1, len(points))]
    t_i = ts[i]
    hist = [r[k - 1] for k in range(1, i) if t_i - timedelta(days=window_days) <= ts[k] < t_i]
    med = np.median(hist)
    return (r[i - 1] - med) / (1.4826 * np.median(np.abs(np.array(hist) - med))), len(hist)


@pytest.mark.parametrize("key", ["EUR/USD", "EUR/JPY", "EUR/CHF", "EUR/TRY"])
def test_real_fx_is_scored_and_flags_match_an_independent_computation(key):
    points = FX[key]
    assert len(points) == 146
    det = detect("fx.rate", key, points)
    assert det.status == "ok" and det.notes["scored"] == 10  # business days in the last 14 days
    flagged = {f.ts for f in det.flags if f.method == "robust_z"}
    latest = points[-1][0]
    for i in range(1, len(points)):
        if points[i][0] <= latest - timedelta(days=14):
            continue
        z, n = expected_z(points, i)
        assert n >= 40
        assert (abs(z) >= 3.5) == (points[i][0] in flagged)


def test_injected_jump_in_a_real_series_is_flagged_by_both_methods():
    points = list(FX["EUR/USD"])
    ts, value = points[-1]
    points[-1] = (ts, value * 1.03)  # a 3% one-day move; EUR/USD rarely moves 1%
    det = detect("fx.rate", "EUR/USD", points)
    by_method = {f.method: f for f in det.flags if f.ts == ts}
    z, _ = expected_z(points, len(points) - 1)
    rz = by_method["robust_z"]
    assert rz.score == pytest.approx(z, abs=1e-3) and rz.score > 10
    assert rz.value == pytest.approx(value * 1.03)
    # Baseline: previous rate moved by the window's median return, so in USD per EUR.
    assert rz.baseline == pytest.approx(points[-2][1], rel=0.005)
    assert rz.attrs == {"rule": "fx", "window_days": 90, "transform": "log_return", "points": 64, "threshold": 3.5}
    iso = by_method["isolation_forest"]
    assert 0.5 < iso.score <= 1.0 and iso.attrs["points"] >= 55


def test_drops_are_flagged_for_two_sided_rules():
    points = list(FX["EUR/CHF"])
    ts, value = points[-1]
    points[-1] = (ts, value * 0.97)
    det = detect("fx.rate", "EUR/CHF", points)
    assert any(f.ts == ts and f.method == "robust_z" and f.score < -3.5 for f in det.flags)


def test_isolation_forest_is_deterministic_and_bounded():
    a = detect("fx.rate", "EUR/TRY", FX["EUR/TRY"])
    b = detect("fx.rate", "EUR/TRY", FX["EUR/TRY"])
    assert a.flags == b.flags
    for f in a.flags:
        if f.method == "isolation_forest":
            assert 0 < f.score <= 1 and f.ts > a.eval_from


def test_isolation_forest_needs_more_history_than_robust_z():
    points = list(FX["EUR/USD"][-60:])  # ~59 returns: enough for robust z (40), not for the forest (55)
    ts, value = points[-1]
    points[-1] = (ts, value * 1.03)
    det = detect("fx.rate", "EUR/USD", points)
    assert det.status == "ok"
    assert {f.method for f in det.flags} == {"robust_z"}


def test_too_little_history_is_never_flagged():
    points = list(FX["EUR/USD"][:35])  # 34 returns < 40
    ts, value = points[-1]
    points[-1] = (ts, value * 1.5)
    det = detect("fx.rate", "EUR/USD", points)
    assert det.status == "insufficient_history" and det.flags == []
    assert det.needed_points == 40 and det.history_points < 40


def test_step1_fixture_series_report_insufficient_history():
    fx = frankfurter.parse(fixture_json("frankfurter_range.json"))  # 2 days
    trade = portwatch.parse_trade(
        [f["attributes"] for f in fixture_json("portwatch_trade.json")["features"]]
    )  # 1 day
    wb = worldbank.parse(fixture_json("worldbank_page1.json")[1])  # 1 year
    groups = {}
    for p in [*fx, *trade, *wb]:
        groups.setdefault((p.series, p.key), []).append((p.ts, p.value))
    assert len(groups) > 10
    for (series, key), pts in groups.items():
        det = detect(series, key, pts)
        assert det.status in ("insufficient_history", "empty"), (series, det.status)
        assert det.flags == []


def test_censored_epss_is_skipped():
    det = detect("epss.score", "CVE-2026-0001", [(day(i), 0.6) for i in range(60)])
    assert det.status == "skipped" and det.flags == []


def test_constant_peg_is_not_scored():
    det = detect("fx.rate", "EUR/BGN", [(day(i), 1.95583) for i in range(120)])
    assert det.flags == [] and det.notes["zero_spread"] > 0 and det.notes["scored"] == 0


# --- news volume -----------------------------------------------------------------


def test_daily_counts_zero_fill_and_skip_outage_days():
    rows = [("a", day(0), 3), ("b", day(0), 1), ("a", day(2), 2)]  # day 1: no article at all
    counts = anomalies.daily_counts(rows, ["a", "b", "c"])
    assert counts["a"] == [(day(0), 3.0), (day(2), 2.0)]
    assert counts["b"] == [(day(0), 1.0), (day(2), 0.0)]
    assert counts["c"] == [(day(0), 0.0), (day(2), 0.0)]


def test_news_volume_flags_rises_only():
    base = [5, 6, 4, 5, 7, 5, 6, 4, 5, 6, 5, 4, 6, 5, 5, 6, 4, 5, 7, 5, 6, 4, 5, 6]
    up = [(day(i), float(v)) for i, v in enumerate(base + [40])]
    down = [(day(i), float(v)) for i, v in enumerate(base + [0])]
    det = detect(anomalies.NEWS_SERIES, "term", up)
    assert [f.ts for f in det.flags if f.method == "robust_z"] == [day(24)]
    assert detect(anomalies.NEWS_SERIES, "term", down).flags == []
