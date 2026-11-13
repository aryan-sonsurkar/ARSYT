from __future__ import annotations

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


class RedditProgramming:
    def __init__(self) -> None:
        self.base_url = "https://www.reddit.com"

    def fetch(self, limit: int = 15) -> list[dict]:
        topics = []
        subreddits = [
            "programming", "webdev", "Python", "rust", "golang",
            "javascript", "compsci", "opensource", "devops",
        ]
        headers = {"User-Agent": "CodeShortsBot/1.0"}
        for sub in subreddits:
            try:
                resp = requests.get(
                    f"{self.base_url}/r/{sub}/hot.json?limit=5",
                    headers=headers, timeout=15,
                )
                resp.raise_for_status()
                for post in resp.json().get("data", {}).get("children", []):
                    data = post.get("data", {})
                    title = data.get("title", "")
                    if self._is_relevant(title, sub):
                        topics.append({
                            "source": f"reddit_r_{sub}",
                            "title": title[:120],
                            "topic": title[:80],
                            "category": self._categorize(title, sub),
                            "url": f"https://reddit.com{data.get('permalink', '')}",
                            "score": data.get("score", 0),
                            "fetched_at": datetime.now().isoformat(),
                        })
            except Exception as e:
                logger.warning("Reddit r/%s error: %s", sub, e)
        logger.info("Fetched %d Reddit posts", len(topics))
        return topics

    def _is_relevant(self, title: str, sub: str) -> bool:
        t = title.lower()
        skip = ["askreddit", "ama ", "help ", "please ", "urgent", "need help"]
        return not any(s in t for s in skip)

    def _categorize(self, title: str, sub: str) -> str:
        t = title.lower()
        sub = sub.lower()
        if sub in ("python", "rust", "golang", "javascript"):
            return "programming_languages"
        if sub == "opensource":
            return "open_source_projects"
        if sub == "compsci":
            return "computer_science_facts"
        if sub == "devops":
            return "developer_productivity"
        if any(kw in t for kw in ["algorithm", "data structure", "complexity", "big o"]):
            return "programming_concepts"
        return "software_engineering_stories"
