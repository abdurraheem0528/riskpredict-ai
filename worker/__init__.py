"""RiskPredict AI worker: source syncs, Drive watch renewal, risk-engine runs,
advice wording, alerts and live-update events. Run once per feeds run (GitHub
Actions) or in a loop (docker compose); the API runs the same passes in requests."""
