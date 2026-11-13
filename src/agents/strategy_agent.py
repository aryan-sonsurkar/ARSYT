from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)


class StrategyAgent:
    """Weekly content strategy analysis and adaptive topic weighting."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()

    def run_weekly_review(self) -> Path:
        """Full weekly strategy pipeline: fetch analytics, compare, report, reweight."""
        logger.info("=" * 60)
        logger.info("Starting weekly strategy review")
        logger.info("=" * 60)

        since = datetime.now() - timedelta(days=7)
        analytics = self.db.get_analytics_for_period(since)

        if not analytics:
            logger.warning("No analytics data for the past 7 days, skipping review")
            return self._write_empty_report()

        comparison = self._compare_performance(analytics)
        hooks = self._analyze_hooks(analytics)
        timing = self._analyze_posting_times(analytics)
        lengths = self._analyze_durations(analytics)
        category_perf = self._analyze_categories(analytics)

        report_path = self._generate_report(
            comparison, hooks, timing, lengths, category_perf, analytics
        )

        self._update_category_weights(category_perf)

        recommendations = self._build_recommendations(
            comparison, hooks, timing, lengths, category_perf
        )
        self.db.save_strategy_report(
            str(report_path), report_path.read_text(encoding="utf-8"),
            json.dumps(recommendations, indent=2),
        )

        logger.info("Weekly strategy review complete: %s", report_path)
        return report_path

    def get_weighted_categories(self) -> dict[str, float]:
        """Return categories with adaptive weights for topic generation."""
        weights = self.db.get_category_weights()
        total = sum(w["weight"] for w in weights)
        if total <= 0:
            return {w["category"]: 1.0 / len(weights) for w in weights}

        capped = {}
        for w in weights:
            raw = w["weight"] / total
            capped[w["category"]] = min(raw, self.settings.max_category_weight)

        remaining = 1.0 - sum(capped.values())
        uncapped_count = sum(
            1 for w in weights if w["weight"] / total < self.settings.max_category_weight
        )
        if uncapped_count > 0 and remaining > 0:
            per_uncapped = remaining / uncapped_count
            for w in weights:
                raw = w["weight"] / total
                if raw < self.settings.max_category_weight:
                    capped[w["category"]] += per_uncapped

        for cat in capped:
            capped[cat] = max(capped[cat], self.settings.min_category_weight)

        total = sum(capped.values())
        return {k: v / total for k, v in capped.items()}

    def _compare_performance(
        self, analytics: list[dict]
    ) -> dict[str, Any]:
        sorted_by_engagement = sorted(
            analytics, key=lambda x: x.get("engagement_rate", 0), reverse=True
        )
        top_n = max(3, len(sorted_by_engagement) // 3)
        top = sorted_by_engagement[:top_n]
        bottom = sorted_by_engagement[-top_n:] if len(sorted_by_engagement) > top_n else []

        top_cats = defaultdict(int)
        for t in top:
            c = t.get("category") or t.get("vid_category", "")
            if c:
                top_cats[c] += 1

        bottom_cats = defaultdict(int)
        for b in bottom:
            c = b.get("category") or b.get("vid_category", "")
            if c:
                bottom_cats[c] += 1

        return {
            "top_performers": [
                {
                    "title": t.get("title") or t.get("vid_title", ""),
                    "category": t.get("category") or t.get("vid_category", ""),
                    "engagement": t.get("engagement_rate", 0),
                    "views": t.get("views", 0),
                    "likes": t.get("likes", 0),
                }
                for t in top
            ],
            "bottom_performers": [
                {
                    "title": b.get("title") or b.get("vid_title", ""),
                    "category": b.get("category") or b.get("vid_category", ""),
                    "engagement": b.get("engagement_rate", 0),
                    "views": b.get("views", 0),
                    "likes": b.get("likes", 0),
                }
                for b in bottom
            ],
            "best_categories": sorted(
                top_cats.items(), key=lambda x: x[1], reverse=True
            ),
            "worst_categories": sorted(
                bottom_cats.items(), key=lambda x: x[1], reverse=True
            ),
        }

    def _analyze_hooks(self, analytics: list[dict]) -> dict[str, Any]:
        top = sorted(
            analytics, key=lambda x: x.get("engagement_rate", 0), reverse=True
        )[:5]
        bottom = sorted(
            analytics, key=lambda x: x.get("engagement_rate", 0)
        )[:5]

        def extract_hook(script: str) -> str:
            if not script:
                return ""
            return script[:120].strip()

        return {
            "best_hooks": [
                extract_hook(a.get("script", "")) for a in top if a.get("script")
            ],
            "worst_hooks": [
                extract_hook(a.get("script", "")) for a in bottom if a.get("script")
            ],
            "recommendation": (
                "Use questions, surprising facts, or bold statements as hooks."
            ),
        }

    def _analyze_posting_times(self, analytics: list[dict]) -> dict[str, Any]:
        hourly_engagement = defaultdict(list)
        for a in analytics:
            upload_date = a.get("upload_date")
            if upload_date:
                try:
                    dt = (
                        datetime.fromisoformat(upload_date)
                        if isinstance(upload_date, str)
                        else upload_date
                    )
                    hourly_engagement[dt.hour].append(
                        a.get("engagement_rate", 0)
                    )
                except (ValueError, TypeError):
                    pass

        hourly_avg = {
            hour: sum(engs) / len(engs)
            for hour, engs in hourly_engagement.items()
        }
        best_hours = sorted(hourly_avg.items(), key=lambda x: x[1], reverse=True)

        return {
            "hourly_engagement": hourly_avg,
            "best_hours": [h for h, _ in best_hours[:3]],
            "worst_hours": [h for h, _ in best_hours[-3:]] if len(best_hours) >= 3 else [],
        }

    def _analyze_durations(self, analytics: list[dict]) -> dict[str, Any]:
        durations = [
            (
                a.get("duration_seconds") or a.get("vid_duration", 0),
                a.get("engagement_rate", 0),
            )
            for a in analytics
            if (a.get("duration_seconds") or a.get("vid_duration", 0)) > 0
        ]
        if not durations:
            return {"recommended_range": "30-60s", "avg_engagement_by_bucket": {}}

        buckets = {"<30s": [], "30-45s": [], "45-60s": [], ">60s": []}
        for dur, eng in durations:
            if dur < 30:
                buckets["<30s"].append(eng)
            elif dur < 45:
                buckets["30-45s"].append(eng)
            elif dur < 60:
                buckets["45-60s"].append(eng)
            else:
                buckets[">60s"].append(eng)

        avg_by_bucket = {
            k: (sum(v) / len(v) if v else 0) for k, v in buckets.items()
        }
        best = max(avg_by_bucket, key=avg_by_bucket.get) if avg_by_bucket else "30-60s"

        return {
            "recommended_range": best,
            "avg_engagement_by_bucket": avg_by_bucket,
        }

    def _analyze_categories(
        self, analytics: list[dict]
    ) -> list[dict[str, Any]]:
        cat_stats: dict[str, dict] = {}
        for a in analytics:
            cat = a.get("category") or a.get("vid_category", "")
            if not cat:
                continue
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "category": cat,
                    "total_views": 0,
                    "total_likes": 0,
                    "total_comments": 0,
                    "total_shares": 0,
                    "total_engagement": 0.0,
                    "count": 0,
                }
            s = cat_stats[cat]
            s["total_views"] += a.get("views", 0) or 0
            s["total_likes"] += a.get("likes", 0) or 0
            s["total_comments"] += a.get("comments", 0) or 0
            s["total_shares"] += a.get("shares", 0) or 0
            s["total_engagement"] += a.get("engagement_rate", 0) or 0
            s["count"] += 1

        for s in cat_stats.values():
            s["avg_engagement"] = s["total_engagement"] / s["count"] if s["count"] else 0
            if s["total_views"] > 0:
                s["engagement_rate"] = (
                    (s["total_likes"] + s["total_comments"] + s["total_shares"])
                    / s["total_views"]
                ) * 100
            else:
                s["engagement_rate"] = 0

        return sorted(
            cat_stats.values(), key=lambda x: x["avg_engagement"], reverse=True
        )

    def _generate_report(
        self,
        comparison: dict,
        hooks: dict,
        timing: dict,
        lengths: dict,
        category_perf: list[dict],
        analytics: list[dict],
    ) -> Path:
        now = datetime.now()
        report_dir = self.settings.reports_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{now.strftime('%Y-%m-%d')}-weekly-report.md"

        lines = [
            f"# Weekly Strategy Report",
            f"**Date:** {now.strftime('%Y-%m-%d')}",
            f"**Period:** {(now - timedelta(days=7)).strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
            f"**Total Videos Analyzed:** {len(analytics)}",
            "",
            "---",
            "",
            "## Top Performing Content",
            "",
        ]

        if comparison["top_performers"]:
            for p in comparison["top_performers"]:
                lines.append(
                    f"- **{p['title']}** [{p['category']}] - "
                    f"Engagement: {p['engagement']:.2f}%, Views: {p['views']}, Likes: {p['likes']}"
                )
        else:
            lines.append("*No top performers identified yet.*")

        lines += [
            "",
            "## Low Performing Content",
            "",
        ]
        if comparison["bottom_performers"]:
            for p in comparison["bottom_performers"]:
                lines.append(
                    f"- **{p['title']}** [{p['category']}] - "
                    f"Engagement: {p['engagement']:.2f}%, Views: {p['views']}, Likes: {p['likes']}"
                )
        else:
            lines.append("*No low performers identified yet.*")

        lines += [
            "",
            "## Category Performance",
            "",
            "| Category | Videos | Avg Engagement | Total Views | Total Likes |",
            "|----------|--------|----------------|-------------|-------------|",
        ]
        for cat in category_perf:
            lines.append(
                f"| {cat['category']} | {cat['count']} | "
                f"{cat['avg_engagement']:.2f}% | {cat['total_views']} | {cat['total_likes']} |"
            )

        lines += [
            "",
            "## Best Categories",
            "",
        ]
        if comparison["best_categories"]:
            for cat, count in comparison["best_categories"]:
                lines.append(f"- {cat}: appeared in {count} top videos")
        else:
            lines.append("*Insufficient data.*")

        lines += [
            "",
            "## Worst Categories",
            "",
        ]
        if comparison["worst_categories"]:
            for cat, count in comparison["worst_categories"]:
                lines.append(f"- {cat}: appeared in {count} low-performing videos")
        else:
            lines.append("*Insufficient data.*")

        lines += [
            "",
            "## Hook Analysis",
            "",
            "### Best Performing Hooks",
            "",
        ]
        for h in hooks.get("best_hooks", []):
            lines.append(f'- "{h}"')
        lines += [
            "",
            "### Worst Performing Hooks",
            "",
        ]
        for h in hooks.get("worst_hooks", []):
            lines.append(f'- "{h}"')
        lines += [
            "",
            f"**Recommendation:** {hooks.get('recommendation', '')}",
            "",
            "## Optimal Video Length",
            "",
            f"**Recommended Range:** {lengths.get('recommended_range', '30-60s')}",
            "",
            "| Bucket | Avg Engagement |",
            "|--------|----------------|",
        ]
        for bucket, eng in lengths.get("avg_engagement_by_bucket", {}).items():
            lines.append(f"| {bucket} | {eng:.2f}% |")

        lines += [
            "",
            "## Optimal Posting Times",
            "",
        ]
        best_hours = timing.get("best_hours", [])
        if best_hours:
            lines.append(f"**Best hours:** {', '.join(f'{h}:00' for h in best_hours)}")
        else:
            lines.append("*Insufficient timing data.*")

        lines += [
            "",
            "## Category Weights (Next Week)",
            "",
        ]
        weights = self.db.get_category_weights()
        for w in weights:
            lines.append(f"- {w['category']}: {w['weight']:.2f}")

        lines += [
            "",
            "---",
            f"*Report generated automatically by CodeShortsBot Strategy Agent*",
            "",
        ]

        content = "\n".join(lines) + "\n"
        report_path.write_text(content, encoding="utf-8")
        logger.info("Weekly strategy report written: %s", report_path)
        return report_path

    def _update_category_weights(self, category_perf: list[dict]) -> None:
        if not category_perf:
            logger.info("No category performance data to update weights")
            return

        max_eng = max(c["avg_engagement"] for c in category_perf) if category_perf else 1
        if max_eng <= 0:
            max_eng = 1

        for cat in category_perf:
            raw_score = cat["avg_engagement"] / max_eng if max_eng > 0 else 0.5
            current = self.db.get_category_weight(cat["category"])
            new_weight = (current * 0.7) + (raw_score * 0.3)
            new_weight = max(self.settings.min_category_weight * 2, new_weight)
            self.db.update_category_weight(
                cat["category"], new_weight, cat["avg_engagement"]
            )

        missing = [
            "mindblowing_concepts",
            "career_secrets",
            "hidden_truths",
            "beginner_mistakes",
            "engineering_marvels",
            "productivity_hacks",
        ]
        existing = {c["category"] for c in category_perf}
        for cat in missing:
            if cat not in existing:
                current = self.db.get_category_weight(cat)
                decayed = current * 0.95
                self.db.update_category_weight(
                    cat, max(self.settings.min_category_weight, decayed), 0.0
                )

        logger.info("Category weights updated based on 7-day performance")

    def _build_recommendations(
        self,
        comparison: dict,
        hooks: dict,
        timing: dict,
        lengths: dict,
        category_perf: list[dict],
    ) -> dict:
        increase = [cat for cat, _ in comparison["best_categories"][:2]] if comparison["best_categories"] else []
        decrease = [cat for cat, _ in comparison["worst_categories"][:2]] if comparison["worst_categories"] else []

        return {
            "generated_at": datetime.now().isoformat(),
            "content_types_to_increase": increase,
            "content_types_to_reduce": decrease,
            "recommended_video_length": lengths.get("recommended_range", "30-60s"),
            "recommended_hook_style": hooks.get("recommendation", ""),
            "recommended_posting_hours": timing.get("best_hours", []),
            "category_performance_ranking": [
                {"category": c["category"], "avg_engagement": c["avg_engagement"]}
                for c in category_perf
            ],
        }

    def _write_empty_report(self) -> Path:
        now = datetime.now()
        report_dir = self.settings.reports_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{now.strftime('%Y-%m-%d')}-weekly-report.md"
        content = (
            f"# Weekly Strategy Report\n"
            f"**Date:** {now.strftime('%Y-%m-%d')}\n\n"
            f"*No analytics data available for the past 7 days.*\n"
            f"*Report generated automatically by CodeShortsBot Strategy Agent*\n"
        )
        report_path.write_text(content, encoding="utf-8")
        return report_path
