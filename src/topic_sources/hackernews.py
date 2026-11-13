from __future__ import annotations

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


class HackerNews:
    def __init__(self) -> None:
        self.base_url = "https://hacker-news.firebaseio.com/v0"

    def fetch(self, limit: int = 5) -> list[dict]:
        topics = []
        try:
            resp = requests.get(f"{self.base_url}/topstories.json", timeout=10)
            resp.raise_for_status()
            story_ids = resp.json()[:limit]
            for sid in story_ids:
                try:
                    sr = requests.get(f"{self.base_url}/item/{sid}.json", timeout=10)
                    sr.raise_for_status()
                    item = sr.json()
                    if not item or item.get("type") != "story":
                        continue
                    title = item.get("title", "")
                    url = item.get("url", "")
                    if self._is_tech_relevant(title):
                        topics.append({
                            "source": "hacker_news",
                            "title": title[:120],
                            "topic": title[:80],
                            "category": self._categorize(title),
                            "url": url or f"https://news.ycombinator.com/item?id={sid}",
                            "score": item.get("score", 0),
                            "fetched_at": datetime.now().isoformat(),
                        })
                except Exception:
                    continue
            logger.info("Fetched %d HN stories", len(topics))
        except Exception as e:
            logger.warning("HN API error: %s", e)
        return topics

    def _is_tech_relevant(self, title: str) -> bool:
        keywords = [
            "python", "rust", "go", "javascript", "typescript", "react", "kubernetes",
            "docker", "linux", "open source", "ai", "ml", "database", "compiler",
            "programming", "software", "web", "api", "git", "security", "devops",
            "startup", "algorithm", "data structure", "interview", "coding",
        ]
        t = title.lower()
        return any(kw in t for kw in keywords)

    def _categorize(self, title: str) -> str:
        t = title.lower()
        if any(kw in t for kw in ["python", "rust", "go", "javascript", "typescript", "java", "c++"]):
            return "programming_languages"
        if any(kw in t for kw in ["open source", "github", "kubernetes", "docker", "linux"]):
            return "open_source_projects"
        if any(kw in t for kw in ["ai", "ml", "machine learning", "deep learning", "neural"]):
            return "computer_science_facts"
        if any(kw in t for kw in ["startup", "interview", "career", "developer", "productivity"]):
            return "developer_productivity"
        if any(kw in t for kw in ["algorithm", "data structure", "compiler", "database"]):
            return "programming_concepts"
        return "software_engineering_stories"
