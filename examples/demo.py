"""Run: uv run python examples/demo.py, then open http://127.0.0.1:8000/stats (login admin / change-me)."""

import asyncio
import random

import uvicorn
from fastapi import FastAPI, HTTPException

from fastapi_stats import StatsCollector, StatsConfig, mount_stats

config = StatsConfig(
    slow_threshold_ms=100,
    storage="sqlite",
    sqlite_path="demo_stats.db",
    basic_auth=("admin", "change-me"),  # placeholder values
)
collector = StatsCollector(config)
app = FastAPI()
app.add_middleware(collector.middleware)
mount_stats(app, collector)


@app.get("/items/{item_id}")
async def item(item_id: int):
    await asyncio.sleep(random.random() * 0.2)  # noqa: S311
    if item_id % 7 == 0:
        raise HTTPException(500, "boom")
    return {"id": item_id}


@app.get("/health")
async def health():
    return "ok"


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
