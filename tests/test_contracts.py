"""docs/contracts/openapi.yaml is valid and agrees with the biz schema.

Enums that exist both as SQL CHECK constraints and as OpenAPI enums are compared,
so a change to one without the other fails here instead of in session B or D.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
validator = pytest.importorskip("openapi_spec_validator")

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "docs/contracts/openapi.yaml"
SQL = (ROOT / "infra/migrations/006_biz_schema.sql").read_text()


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text())


def sql_enum(table: str, column: str) -> list[str]:
    start = SQL.index(f"CREATE TABLE biz.{table} (")
    end = SQL.find("CREATE TABLE", start + 1)
    body = SQL[start : end if end > 0 else len(SQL)]
    m = re.search(rf"CHECK \({column} IN\s*\((.*?)\)\)", body, re.S)
    assert m, f"no CHECK ({column} IN ...) in biz.{table}"
    return re.findall(r"'([^']+)'", m.group(1))


def schema(spec: dict, name: str) -> dict:
    return spec["components"]["schemas"][name]


def test_spec_is_valid_openapi_3_1(spec):
    assert spec["openapi"].startswith("3.1")
    validator.validate(spec)


def test_every_operation_has_a_unique_id_and_summary(spec):
    ids = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                assert op.get("summary"), f"{method} {path}"
                ids.append(op["operationId"])
    assert len(ids) == len(set(ids))


def test_workspace_scoped_operations_take_the_header(spec):
    """Everything that touches biz data (except auth, workspaces, webhooks) needs X-Workspace-Id."""
    header = "#/components/parameters/WorkspaceHeader"
    scoped = ("/v1/connections", "/v1/sources", "/v1/entities", "/v1/risks", "/v1/scenarios", "/v1/alerts",
              "/v1/map/entities", "/v1/map/risks", "/v1/events")  # fmt: skip
    for path, item in spec["paths"].items():
        if not path.startswith(scoped) or path in ("/v1/connections/providers", "/v1/connections/google/callback"):
            continue
        for method, op in item.items():
            if method in {"get", "post", "patch", "delete"}:
                refs = [p.get("$ref") for p in item.get("parameters", []) + op.get("parameters", [])]
                assert header in refs, f"{method} {path}"


@pytest.mark.parametrize(
    "table, column, schema_name",
    [
        ("risks", "category", "RiskCategory"),
        ("risks", "status", "RiskStatus"),
        ("entities", "kind", "EntityKind"),
        ("entities", "criticality", "Criticality"),
        ("memberships", "role", "Role"),
        ("connections", "status", "ConnectionStatus"),
        ("sources", "status", "SourceStatus"),
    ],
)
def test_sql_and_openapi_enums_match(spec, table, column, schema_name):
    assert sql_enum(table, column) == schema(spec, schema_name)["enum"]


def test_other_enums_match(spec):
    props = schema(spec, "Alert")["properties"]
    assert sql_enum("alerts", "kind") == props["kind"]["enum"]
    assert sql_enum("alerts", "severity") == props["severity"]["enum"]
    assert sql_enum("risk_evidence", "match_type") == schema(spec, "Evidence")["properties"]["match_type"]["enum"]
    assert sql_enum("sources", "kind") == schema(spec, "Source")["properties"]["kind"]["enum"]
    assert sql_enum("column_mappings", "status") == schema(spec, "ColumnMapping")["properties"]["status"]["enum"]
    assert sql_enum("scenarios", "status") == schema(spec, "ScenarioSummary")["properties"]["status"]["enum"]
    history = schema(spec, "RiskDetail")["allOf"][1]["properties"]["history"]["items"]["properties"]
    assert sql_enum("risk_history", "reason") == history["reason"]["enum"]
    feeds_types = re.search(r"event_type\s+text\s+NOT NULL CHECK \(event_type IN \((.*?)\)\)",
                            (ROOT / "infra/migrations/003_analysis.sql").read_text(), re.S)  # fmt: skip
    assert re.findall(r"'([^']+)'", feeds_types.group(1)) == schema(spec, "TaggedEventType")["enum"]


def test_stream_events_documented_once(spec):
    names = schema(spec, "StreamEventName")["enum"]
    desc = spec["paths"]["/v1/events"]["get"]["description"]
    assert re.findall(r"^\| `([a-z_.]+)` \|", desc, re.M) == names


def test_stored_event_names_match_the_contract_and_the_api(spec):
    """biz.events' CHECK (migration 050) and api/events.py list the contract's names; `resync` is never stored."""
    from api import events

    sql = (ROOT / "infra/migrations/050_serverless_state.sql").read_text()
    body = sql[sql.index("CREATE TABLE biz.events (") :]
    stored = re.findall(r"'([^']+)'", re.search(r"CHECK \(event IN\s*\((.*?)\)\)", body, re.S).group(1))
    assert stored + ["resync"] == schema(spec, "StreamEventName")["enum"]
    assert list(events.EVENT_NAMES) == stored


def test_no_stream_or_internal_endpoints(spec):
    assert not [p for p in spec["paths"] if p.startswith(("/v1/stream", "/internal"))]
    assert "internalToken" not in spec["components"]["securitySchemes"]
