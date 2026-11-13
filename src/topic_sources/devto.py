from __future__ import annotations

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


class DevTo:
    def __init__(self) -> None:
        self.api_url = "https://dev.to/api/articles"

    def fetch(self, limit: int = 15) -> list[dict]:
        topics = []
        try:
            params = {
                "per_page": limit,
                "tag": "programming",
                "state": "rising",
            }
            resp = requests.get(self.api_url, params=params, timeout=15)
            resp.raise_for_status()
            for item in resp.json():
                title = item.get("title", "")
                tags = item.get("tag_list", [])
                topics.append({
                    "source": "devto",
                    "title": title[:120],
                    "topic": title[:80],
                    "category": self._categorize(tags, title),
                    "url": item.get("url", ""),
                    "score": item.get("positive_reactions_count", 0),
                    "fetched_at": datetime.now().isoformat(),
                })
            logger.info("Fetched %d Dev.to articles", len(topics))
        except Exception as e:
            logger.warning("Dev.to API error: %s", e)
        return topics

    def _categorize(self, tags: list[str], title: str) -> str:
        t = title.lower()
        all_tags = " ".join(tags).lower()
        if any(kw in all_tags or kw in t for kw in ["python", "rust", "go", "javascript", "typescript"]):
            return "programming_languages"
        if any(kw in all_tags or kw in t for kw in ["opensource", "github", "docker", "kubernetes", "linux"]):
            return "open_source_projects"
        if any(kw in all_tags or kw in t for kw in ["ai", "machinelearning", "deeplearning"]):
            return "computer_science_facts"
        if any(kw in all_tags or kw in t for kw in ["career", "productivity", "interview", "beginners"]):
            return "developer_productivity"
        if any(kw in all_tags or kw in t for kw in ["algorithms", "datastructures", "tutorial"]):
            return "programming_concepts"
        return "software_engineering_stories"
