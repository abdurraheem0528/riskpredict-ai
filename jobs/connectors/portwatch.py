"""IMF PortWatch via its public ArcGIS REST FeatureServer layers.

* ``PortWatch_ports_database`` – port id → UN/LOCODE / country (lookup only)
* ``portwatch_disruptions_database`` – port disruption events
* ``Daily_Trade_Data_REG`` – daily port calls / import / export per country

All layers are read page by page (``resultOffset``) until ArcGIS stops
reporting ``exceededTransferLimit``. Note: the per-port and per-chokepoint
daily layers (``Daily_Ports_Data``, ``Daily_Chokepoints_Data``) now require an
ArcGIS token, so they are not used.

Disruption severity maps PortWatch's alert level ordinally:
GREEN → 1/3, ORANGE → 2/3, RED → 1.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import httpx

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, parse_date, safe_build, utc_from_ms
from jobs.http import SourceError, get_json
from jobs.models import Event, SeriesPoint, Status

BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PORTS = f"{BASE}/PortWatch_ports_database/FeatureServer/0/query"
DISRUPTIONS = f"{BASE}/portwatch_disruptions_database/FeatureServer/0/query"
TRADE = f"{BASE}/Daily_Trade_Data_REG/FeatureServer/0/query"
PAGE_SIZE = 1000
MAX_PAGES = 50
TRADE_DAYS = 21  # PortWatch daily data lags by roughly ten days
ALERT_SEVERITY = {"GREEN": 1 / 3, "ORANGE": 2 / 3, "RED": 1.0}
TRADE_SERIES = {
    "portcalls": ("portwatch.port_calls", "calls"),
    "import": ("portwatch.import_volume", "metric tonnes"),
    "export": ("portwatch.export_volume", "metric tonnes"),
}


def query_all(
    client: httpx.Client,
    url: str,
    ctx: RunContext,
    *,
    where: str = "1=1",
    out_fields: str = "*",
    order_by: str = "ObjectId",
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "false",
            "orderByFields": order_by,
            "resultOffset": page * page_size,
            "resultRecordCount": page_size,
            "f": "json",
        }
        payload = get_json(client, f"{url}?{urlencode(params)}", sleep=ctx.sleep)
        if "error" in payload:
            code = (payload.get("error") or {}).get("code")
            raise SourceError(f"ArcGIS error {code} from services9.arcgis.com")
        rows.extend(f.get("attributes") or {} for f in payload.get("features") or [])
        if not payload.get("exceededTransferLimit"):
            return rows
    raise SourceError("ArcGIS pagination did not finish")


class PortIndex:
    """Port id → LOCODE and country-name → ISO3, both taken from PortWatch."""

    def __init__(self, ports: list[dict]):
        self.locode: dict[str, str] = {}
        self.country: dict[str, str] = {}
        for p in ports:
            lc = codes.locode(p.get("LOCODE"))
            if p.get("portid") and lc:
                self.locode[p["portid"]] = lc
            iso3 = p.get("ISO3")
            if p.get("country") and iso3 and codes.is_country(iso3):
                self.country[p["country"].strip()] = iso3

    def country_codes(self, names: str | None) -> list[str]:
        out = []
        for n in (names or "").split(";"):
            n = n.strip()
            code = self.country.get(n) or codes.country_alpha3(n)
            if code:
                out.append(code)
        return out

    def port_codes(self, ids: str | None) -> list[str]:
        return [self.locode[i.strip()] for i in (ids or "").split(";") if i.strip() in self.locode]


def parse_disruptions(rows: list[dict], index: PortIndex, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    events = []
    for r in rows:
        if r.get("eventid") is None:
            continue
        lat, lon = r.get("lat"), r.get("long")
        ev = safe_build(
            Event,
            notes,
            source="portwatch",
            raw_id=f"{r.get('eventtype') or 'NA'}-{r['eventid']}",
            type="port.disruption",
            title=r.get("eventname") or r.get("htmlname") or "",
            published_at=utc_from_ms(r.get("fromdate")),
            countries=index.country_codes(r.get("country")),
            ports=index.port_codes(r.get("affectedports")),
            severity=ALERT_SEVERITY.get((r.get("alertlevel") or "").upper()),
            lat=lat if lat is not None and lon is not None else None,
            lon=lon if lat is not None and lon is not None else None,
            attrs={
                k: v
                for k, v in {
                    "hazard_type": r.get("eventtype"),
                    "alert_level": r.get("alertlevel"),
                    "severity_text": r.get("severitytext"),
                    "description": r.get("htmldescription"),
                    "to_date": utc_from_ms(r.get("todate")).isoformat() if r.get("todate") else None,
                    "affected_ports": r.get("n_affectedports"),
                    "affected_population": r.get("affectedpopulation"),
                }.items()
                if v not in (None, "")
            },
        )
        if ev:
            events.append(ev)
    return events


def parse_trade(rows: list[dict], notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    """Country-level rows only; regional aggregates (numeric codes, WLD) are skipped."""
    notes = notes if notes is not None else {}
    points = []
    for r in rows:
        iso3 = r.get("ISO3")
        if not iso3 or not codes.is_country(iso3):
            continue
        day = r.get("date")
        # ArcGIS returns DateOnly fields as "YYYY-MM-DD" (or epoch ms on older layers).
        ts = utc_from_ms(day) if isinstance(day, (int, float)) else parse_date(day)
        if ts is None:
            continue
        for field_name, (series, unit) in TRADE_SERIES.items():
            if r.get(field_name) is None:
                continue
            p = safe_build(
                SeriesPoint, notes, source="portwatch", series=series, key=iso3, ts=ts, value=r[field_name], unit=unit
            )
            if p:
                points.append(p)
    return points


class PortWatchConnector(Connector):
    name = "portwatch"
    min_interval = timedelta(hours=6)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        errors = []
        index = PortIndex(query_all(client, PORTS, ctx, out_fields="portid,country,ISO3,LOCODE"))
        batch.notes["ports_indexed"] = len(index.locode)
        try:
            rows = query_all(client, DISRUPTIONS, ctx, page_size=2000)
            batch.events = parse_disruptions(rows, index, batch.notes)
        except SourceError as exc:
            errors.append(str(exc))
        try:
            since = (ctx.now - timedelta(days=TRADE_DAYS)).strftime("%Y-%m-%d")
            rows = query_all(
                client,
                TRADE,
                ctx,
                where=f"date >= '{since}'",
                out_fields="ISO3,date,portcalls,import,export",
            )
            batch.series = parse_trade(rows, batch.notes)
        except SourceError as exc:
            errors.append(str(exc))
        if errors:
            batch.error = "; ".join(errors)
            batch.status = Status.ERROR if len(errors) == 2 else Status.PARTIAL
        return batch
