"""RiskPredict AI API: business data, risks and map layers (docs/contracts/openapi.yaml).

``api.core`` holds the pieces the worker shares (configuration, database access,
encryption, the Google / Composio / Groq clients, column mapping and the
serialisers). It never imports FastAPI, so the worker image does not need it.
"""

__version__ = "0.1.0"
