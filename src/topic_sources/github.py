from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class GitHubTrending:
    def __init__(self) -> None:
        self.api_url = "https://api.github.com/search/repositories"

    def fetch(self, since: str = "weekly") -> list[dict]:
        topics = []
        try:
            params = {
                "q": "stars:>100 pushed:>2024-01-01",
                "sort": "stars",
                "order": "desc",
                "per_page": 10,
            }
            headers = {"Accept": "application/vnd.github.v3+json"}
            resp = requests.get(self.api_url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                topics.append({
                    "source": "github_trending",
                    "title": item.get("description", item["name"])[:120],
                    "topic": item["name"],
                    "category": "open_source_projects",
                    "url": item["html_url"],
                    "score": item.get("stargazers_count", 0),
                    "fetched_at": datetime.now().isoformat(),
                })
            logger.info("Fetched %d GitHub trending repos", len(topics))
        except Exception as e:
            logger.warning("GitHub API error: %s", e)
        return topics
