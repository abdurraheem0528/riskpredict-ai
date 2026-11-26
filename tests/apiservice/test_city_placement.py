"""A workspace synced before city placement is corrected by the worker's next re-read, with
nothing done by its owner: the row digest includes APPLY_RULES_VERSION, so the re-read sees
changed content, re-syncs the rows (now placed at their city) and re-scores. Test rows only."""

from __future__ import annotations

import hashlib
import json
import uuid
from uuid import UUID

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API
pytest.importorskip("engine")

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

HEADER = ["Supplier ID", "Supplier", "Country", "City", "Main port"]
ROW = ["SUP-001", "Formosa Microcircuits", "Taiwan", "Hsinchu", "TWKHH"]
SPEC = {"entity_kind": "supplier", "columns": [
    {"column": "Supplier ID", "field": "row_key"}, {"column": "Supplier", "field": "name"},
    {"column": "Country", "field": "country"}, {"column": "City", "field": "city"}, {"column": "Main port", "field": "port"},
]}  # fmt: skip


def test_the_next_re_read_moves_an_entity_from_its_port_to_its_city(client, owner_db, settings, monkeypatch):
    from api.core import mapping, providers
    from worker import sync
    from worker.main import Worker

    me = sign_up(client, workspace="Places")
    ws = me["workspace_id"]
    values = dict(zip(HEADER, ROW))
    old_digest = hashlib.sha256(json.dumps({"rules": 2, "values": values}, sort_keys=True).encode()).hexdigest()
    conn, src, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owner_db.execute("INSERT INTO biz.connections (id, workspace_id, provider, status, composio_connected_account_id)"
                     " VALUES (%s, %s, 'composio:airtable', 'active', 'ca_places')", (conn, ws))  # fmt: skip
    owner_db.execute("INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, external_table_id,"
                     " name, status, last_synced_at, content_hash) VALUES (%s, %s, %s, 'composio_table', 'appP', 'tbl',"
                     " 'Suppliers', 'active', now(), 'old')", (src, ws, conn))  # fmt: skip
    owner_db.execute("INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
                     " VALUES (%s, %s, %s, 1, 'confirmed', %s, now())", (mid, ws, src, json.dumps(SPEC)))  # fmt: skip
    # As the previous release wrote it: no position, so the dashboard put it at the port.
    owner_db.execute("INSERT INTO biz.entities (workspace_id, source_id, mapping_id, source_row_key, source_row_number,"
                     " source_row_hash, kind, name, countries, ports, city) VALUES (%s, %s, %s, 'SUP-001', 2, %s,"
                     " 'supplier', 'Formosa Microcircuits', '{TWN}', '{TWKHH}', 'Hsinchu')",
                     (ws, src, mid, old_digest))  # fmt: skip
    owner_db.execute("INSERT INTO feeds.events (source, raw_id, type, title, countries, severity, lat, lon, attrs)"
                     " VALUES ('usgs', 'q-hsinchu', 'hazard.earthquake', 'M6 near Hsinchu', '{TWN}', 0.8, 24.85, 121.05,"
                     " '{\"magnitude\": 6.0}')")  # fmt: skip

    table = providers.Table("Suppliers", None, [{"name": h, "samples": []} for h in HEADER],
                            [mapping.RawRow(2, None, values)], HEADER)  # fmt: skip
    monkeypatch.setattr(sync, "read_for_recheck", lambda *a, **k: table)
    worker = Worker(settings)
    worker.db.open()
    try:
        worker._refresh_cache(UUID(ws))
        assert worker.recheck() == 1
    finally:
        worker.db.close()

    [e] = client.get("/v1/entities", headers=hdr(me)).json()["items"]
    assert (e["lat"], e["lon"]) == (24.8036, 120.9686) and e["ports"] == ["TWKHH"]  # Hsinchu; the port is kept
    assert e["place"] == "Hsinchu, Taiwan"
    [r] = client.get("/v1/risks", headers=hdr(me)).json()["items"]
    assert r["title"] == "Earthquake near Formosa Microcircuits" and (r["lat"], r["lon"]) == (24.8036, 120.9686)
    assert r["place"] == "Hsinchu, Taiwan" and r["entity"]["city"] == "Hsinchu"
    [f] = client.get("/v1/map/entities", headers=hdr(me)).json()["features"]
    assert f["geometry"]["coordinates"] == [120.9686, 24.8036]
