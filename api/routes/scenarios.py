"""What-if runs: ``engine.run_scenario`` on the workspace's entities. Writes only
the ``biz.scenarios`` row; real risks are untouched."""

from __future__ import annotations

import logging
from typing import Annotated, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from api import pagination
from api.context import WsCtx
from api.core import engine as engine_iface
from api.core import serialize as ser
from api.core.errors import not_found

log = logging.getLogger("riskpredict.scenarios")
router = APIRouter(tags=["scenarios"])

Country = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Locode = Annotated[str, Field(pattern=r"^[A-Z]{2}[A-Z2-9]{3}$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Category = Literal["natural_hazard", "logistics", "geopolitical", "sanctions", "trade_policy", "cyber", "financial",
                   "labour", "other"]  # fmt: skip


class HazardShock(BaseModel):
    type: Literal["event"]
    category: Category
    severity: float = Field(ge=0, le=1)
    label: str | None = Field(default=None, max_length=200)
    countries: list[Country] | None = None
    ports: list[Locode] | None = None
    chokepoints: list[str] | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, ge=1, le=2000)


class FxShock(BaseModel):
    type: Literal["fx"]
    currency: Currency
    base_currency: Currency | None = None
    pct_change: float = Field(ge=-90, le=500)


class SanctionsShock(BaseModel):
    type: Literal["sanction"]
    entity_id: UUID


class Override(BaseModel):
    entity_id: UUID
    exclude: bool | None = None
    criticality: Literal["low", "medium", "high", "critical"] | None = None
    annual_spend: str | None = Field(default=None, pattern=r"^\d+(\.\d{1,2})?$")
    revenue_share: float | None = Field(default=None, ge=0, le=1)
    countries: list[Country] | None = None


class ScenarioInputs(BaseModel):
    shocks: list[Annotated[Union[HazardShock, FxShock, SanctionsShock], Field(discriminator="type")]] | None = Field(
        default=None, max_length=20
    )
    entity_overrides: list[Override] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one(self):
        if not self.shocks and not self.entity_overrides:
            raise ValueError("give at least one of shocks or entity_overrides")
        return self


class NewScenario(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    inputs: ScenarioInputs


def engine_inputs(eng, inputs: dict):
    """The engine parses ScenarioInputs into its own dataclasses; prefer its parser."""
    cls = getattr(eng, "ScenarioInputs", None)
    for name in ("from_dict", "parse", "from_json"):
        if cls is not None and callable(getattr(cls, name, None)):
            return getattr(cls, name)(inputs)
    return inputs


@router.get("/v1/scenarios")
def list_scenarios(
    ctx: WsCtx, limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: Annotated[str | None, Query()] = None
) -> dict:
    after = pagination.decode(cursor, 2)
    with ctx.tx() as conn:
        rows = conn.execute(
            "SELECT id, name, description, status, created_at, completed_at FROM biz.scenarios"
            " WHERE (%(at)s::text IS NULL OR (created_at, id) < (%(at)s::timestamptz, %(ai)s::uuid))"
            " ORDER BY created_at DESC, id DESC LIMIT %(n)s",
            {"at": str(after[0]) if after else None, "ai": after[1] if after else None, "n": limit + 1},
        ).fetchall()
    return pagination.page(rows, limit, ["created_at", "id"], ser.scenario_summary)


@router.post("/v1/scenarios", status_code=201)
def run_scenario(body: NewScenario, ctx: WsCtx) -> dict:
    inputs = body.inputs.model_dump(mode="json", exclude_none=True)
    with ctx.tx("editor") as conn:
        row = conn.execute(
            "INSERT INTO biz.scenarios (workspace_id, name, description, inputs, created_by)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (ctx.workspace_id, body.name, body.description, Jsonb(inputs), ctx.user_id),
        ).fetchone()
    sid = row["id"]
    eng = engine_iface.load()
    results, error, version = None, None, None
    if eng is None:
        error = "the risk engine is not installed on this server"
    else:
        try:
            with ctx.tx("editor") as conn:
                conn.execute("SET LOCAL statement_timeout = '20s'")
                results = eng.run_scenario(conn, ctx.workspace_id, engine_inputs(eng, inputs))
            version = (results or {}).get("engine_version") or getattr(eng, "ENGINE_VERSION", None)
        except Exception as exc:  # report, don't crash: the row records the failure
            log.warning("scenario run failed (%s)", type(exc).__name__)
            error = "the scenario could not be run"
            results = None
    with ctx.tx("editor") as conn:
        out = conn.execute(
            "UPDATE biz.scenarios SET status = %s, results = %s, error = %s, engine_version = %s, completed_at = now()"
            " WHERE id = %s RETURNING *",
            ("completed" if results is not None else "failed", Jsonb(results) if results is not None else None,
             error, version, sid),
        ).fetchone()  # fmt: skip
    return ser.scenario(out)


@router.get("/v1/scenarios/{scenario_id}")
def get_scenario(scenario_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        row = conn.execute("SELECT * FROM biz.scenarios WHERE id = %s", (scenario_id,)).fetchone()
    if row is None:
        raise not_found("scenario not found")
    return ser.scenario(row)


@router.delete("/v1/scenarios/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: UUID, ctx: WsCtx) -> Response:
    with ctx.tx("editor") as conn:
        if conn.execute("DELETE FROM biz.scenarios WHERE id = %s RETURNING id", (scenario_id,)).fetchone() is None:
            raise not_found("scenario not found")
    return Response(status_code=204)
