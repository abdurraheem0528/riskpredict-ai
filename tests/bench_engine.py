"""Time one engine run on a synthetic, deliberately large workspace (test data only).

    TEST_DATABASE_URL=postgresql://test:test@localhost:5432/feeds_test python tests/bench_engine.py

It wipes and re-migrates that database (throwaway only!), loads feed rows at
roughly the volume the free tier holds after 90 days of retention plus a full
OFAC list, creates a workspace with a few hundred entities, and prints the time
of each phase of ``run_workspace`` as an rp_app login. Not collected by pytest.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

import engine  # noqa: E402
from engine_fixtures import NOW, event, fx_history, news, ofac, quake, tag  # noqa: E402
from test_engine_db import make_workspace, put_events, roles_script  # noqa: E402

DSN = os.environ["TEST_DATABASE_URL"]
CCYS = ["USD", "GBP", "JPY", "CNY", "TRY", "INR", "BRL", "ZAR", "KRW", "MXN"]
COUNTRIES = ["JPN", "CHN", "DEU", "USA", "TUR", "IND", "BRA", "ZAF", "KOR", "MEX", "VNM", "IDN", "THA", "MMR", "ARE"]
WORDS = "acme global nordic pacific delta prime united atlas zenith orion apex vertex summit harbor beacon".split()


def lcg(seed: int):
    x = seed
    while True:
        x = (1103515245 * x + 12345) % 2**31
        yield x / 2**31


def main(n_entities: int = 400, n_news: int = 8000, n_hazards: int = 3000, n_ofac: int = 20000) -> None:
    from conftest import reset_database
    from jobs import migrate

    r = lcg(42)
    reset_database(DSN)
    migrate.apply(DSN)
    t0 = time.perf_counter()
    evs = []
    for i in range(n_hazards):
        evs.append(quake(f"hz{i}", next(r) * 140 - 60, next(r) * 360 - 180, magnitude=4.5 + 2 * next(r),
                         severity=next(r), age_h=next(r) * 24 * 30))  # fmt: skip
    for i in range(n_news):
        c = COUNTRIES[int(next(r) * len(COUNTRIES))]
        name = f"{WORDS[int(next(r) * 15)]} {WORDS[int(next(r) * 15)]}"
        evs.append(news(f"n{i}", tag("conflict" if i % 3 else "labour_action", next(r), countries=[c],
                                     companies=[(name, next(r))], places=[("Red Sea", 0.7)] if i % 50 == 0 else []),
                        age_h=next(r) * 24 * 25))  # fmt: skip
    for i in range(n_ofac):
        name = f"{WORDS[int(next(r) * 15)]} {WORDS[int(next(r) * 15)]} {i} trading"
        evs.append(ofac(f"sdn{i}", name, type_="sanction.listed", aliases=[name + " llc"], age_h=24 * 400,
                        countries=[COUNTRIES[i % 15]]))  # fmt: skip
    for i in range(800):
        evs.append(event("osv", f"GHSA-{i}", "cyber.vulnerability", software=[f"PyPI:pkg{i % 80}"], severity=next(r),
                         age_h=next(r) * 24 * 300))  # fmt: skip
    put_events(*evs)
    with psycopg.connect(DSN) as c:
        for ccy in CCYS:
            for (name, key), pts in fx_history(ccy, start=1 + 10 * next(r)).items():
                for ts, v in pts:
                    c.execute("INSERT INTO feeds.series (series, key, ts, source, value) VALUES (%s,%s,%s,'f',%s)",
                              (name, key, ts, v))  # fmt: skip
        c.execute("ANALYZE")
    print(f"feeds loaded: {len(evs)} events in {time.perf_counter() - t0:.1f}s")

    pw = secrets.token_urlsafe(24)
    login = "bench_engine_app_login"
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
        roles_script.ensure_login_role(c, login, "rp_app", pw)
    params = conninfo_to_dict(DSN)
    params.update(user=login, password=pw)
    app = make_conninfo(**params)
    ents = []
    kinds = ["supplier"] * 5 + ["customer"] * 2 + ["site", "route", "software", "currency"]
    for i in range(n_entities):
        k = kinds[i % len(kinds)]
        e = {"kind": k, "name": f"{WORDS[i % 15]} {WORDS[(i * 7) % 15]} {i} ltd", "countries": [COUNTRIES[i % 15]]}
        if k in ("supplier", "site"):
            e.update(lat=next(r) * 140 - 60, lon=next(r) * 360 - 180, criticality="high",
                     annual_spend=int(1000 * next(r)) + 1, spend_currency=CCYS[i % len(CCYS)])  # fmt: skip
        if k == "route":
            e.update(ports=["CNSHA", "NLRTM"], attrs={"chokepoints": ["bab_el_mandeb", "suez"]})
        if k == "software":
            e.update(software=[f"pkg:pypi/pkg{i % 100}@1"], countries=[])
        if k == "currency":
            e.update(currencies=[CCYS[i % len(CCYS)]], countries=[])
        ents.append(e)
    ws = make_workspace(app, "bench", ents, base_currency="USD")["ws"]

    def phase(label, fn):
        t = time.perf_counter()
        out = fn()
        print(f"  {label:<14}{(time.perf_counter() - t) * 1000:8.0f} ms")
        return out

    for run_no in (1, 2):
        print(f"run {run_no} ({'cold: sanctions list read' if run_no == 1 else 'warm: sanctions cached'})")
        t = time.perf_counter()
        with psycopg.connect(app) as c:
            c.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws),))
            entities = phase("load_entities", lambda: engine.load_entities(c))
            feeds = phase("load_feeds", lambda: engine.load_feeds(c, entities, now=NOW))
            out = phase("assess", lambda: engine.assess(entities, feeds, base_currency="USD"))
            res = phase("write_risks", lambda: engine.write_risks(c, ws, out, now=NOW + timedelta(seconds=run_no)))
        print(f"  {'total':<14}{(time.perf_counter() - t) * 1000:8.0f} ms   events read {len(feeds.events)},"
              f" risks {len(out)}, evidence {sum(len(a.evidence) for a in out)}, counts {dict(res.counts)}")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(login)))
        c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))



if __name__ == "__main__":
    main()
