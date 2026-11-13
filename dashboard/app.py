from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from config.settings import get_settings
from database.repository import Repository
from src.system.health_monitor import HealthMonitor

logger = logging.getLogger(__name__)

settings = get_settings()
repo = Repository()
health = HealthMonitor()

app = FastAPI(title="CodeShortsBot Dashboard")

templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    videos = repo.get_all_videos()
    total = len(videos)
    uploaded = sum(
        1 for v in videos if v.uploaded_youtube
    )
    failed = sum(1 for v in videos if v.status == "failed")
    pending = sum(1 for v in videos if v.status not in ("completed", "failed"))

    recent = [v for v in videos if v.created_at][:20]

    cat_weights = repo.get_category_weights()
    weights_data = {w["category"]: round(w["weight"], 2) for w in cat_weights}

    health_results = health.check_all()

    analytics = repo.get_analytics_for_period(
        datetime.now() - timedelta(days=30)
    )
    total_views = sum(a.get("views", 0) or 0 for a in analytics)
    total_likes = sum(a.get("likes", 0) or 0 for a in analytics)
    avg_eng = (
        sum(a.get("engagement_rate", 0) or 0 for a in analytics)
        / max(len(analytics), 1)
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total_videos": total,
            "uploaded": uploaded,
            "failed": failed,
            "pending": pending,
            "recent_videos": recent,
            "category_weights": weights_data,
            "health": health_results,
            "total_views": total_views,
            "total_likes": total_likes,
            "avg_engagement": round(avg_eng, 2),
        },
    )


@app.get("/api/health")
async def api_health():
    return health.check_all()


@app.get("/api/stats")
async def api_stats():
    videos = repo.get_all_videos()
    analytics = repo.get_analytics_for_period(
        datetime.now() - timedelta(days=30)
    )
    return {
        "total_videos": len(videos),
        "uploaded": sum(1 for v in videos if v.uploaded_youtube),
        "failed": sum(1 for v in videos if v.status == "failed"),
        "total_views": sum(a.get("views", 0) or 0 for a in analytics),
        "total_likes": sum(a.get("likes", 0) or 0 for a in analytics),
    }


def serve() -> None:
    uvicorn.run(
        "dashboard.app:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )


if __name__ == "__main__":
    serve()
