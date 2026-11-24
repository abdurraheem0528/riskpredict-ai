# worker

Background worker (runs on Northflank): source syncs, Drive watch renewal, risk-engine
runs per workspace ([`docs/contracts/risk-engine.md`](../docs/contracts/risk-engine.md)),
Groq advice wording, alerts, and SSE events pushed to the API. Connects as an `rp_app`
login. Arrives with session B.
