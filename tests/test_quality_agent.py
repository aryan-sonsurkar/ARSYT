from __future__ import annotations

from src.agents.quality_agent import QualityAgent


class TestQualityAgent:
    def setup_method(self):
        self.agent = QualityAgent()

    def test_score_hook_strong(self):
        score = self.agent._score_hook(
            "Did you know Python's GIL is actually removable? Here's why it matters..."
        )
        assert score > 5.0
        assert score <= 10.0

    def test_score_hook_weak(self):
        score = self.agent._score_hook("Today we will learn about Python.")
        assert score <= 6.0

    def test_score_educational(self):
        score = self.agent._score_educational(
            "The quicksort algorithm works by partitioning an array around a pivot element."
        )
        assert score > 5.0

    def test_score_originality(self):
        score = self.agent._score_originality(
            "Unique perspective on Rust's borrow checker", "Rust borrow checker"
        )
        assert score > 3.0

    def test_score_engagement(self):
        score = self.agent._score_engagement(
            "What would you do? Comment below! This changes everything!"
        )
        assert score > 5.0

    def test_score_clarity(self):
        score = self.agent._score_clarity(
            "Short clear sentences make learning easy. Just three words here."
        )
        assert score > 5.0

    def test_score_script(self):
        result = self.agent.score_script(
            "Test Title",
            "Did you know? This is a test script with educational content about algorithms.",
            "Test Topic",
            "programming_concepts",
        )
        assert "scores" in result
        assert "overall" in result
        assert "passed" in result
        assert all(k in result["scores"] for k in
                   ["hook", "educational_value", "originality", "engagement_potential", "clarity"])
