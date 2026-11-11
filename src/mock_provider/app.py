"""Mock payment provider. Skeleton in M1; full behaviour (section 10) arrives in M2."""

import os

from fastapi import FastAPI

PROVIDER_NAME = os.environ.get("MOCK_PROVIDER_NAME", "mock")

app = FastAPI(title=f"Mock provider {PROVIDER_NAME}")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "provider": PROVIDER_NAME}
