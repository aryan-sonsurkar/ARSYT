from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.topic_intelligence import TopicIntelligenceEngine


class TestTopicIntelligence:
    @patch.object(TopicIntelligenceEngine, "_ollama")
    def test_score_curiosity(self, mock_ollama):
        engine = TopicIntelligenceEngine()
        score = engine._score_curiosity("Why Rust?", "Did you know?")
        assert score >= 5.0
        assert score <= 10.0

    def test_score_curiosity_question_hook(self):
        engine = TopicIntelligenceEngine()
        score = engine._score_curiosity("Topic", "What if you could?")
        assert score > 7.0

    def test_score_novelty(self):
        engine = TopicIntelligenceEngine()
        score = engine._score_novelty("Under the Hood of Kubernetes Scheduler")
        assert score >= 6.0

    def test_score_novelty_basic_topic(self):
        engine = TopicIntelligenceEngine()
        score = engine._score_novelty("What is Python beginner tutorial")
        assert score <= 5.0

    def test_score_educational(self):
        engine = TopicIntelligenceEngine()
        score = engine._score_educational_value("How HashMap works internally O(1)")
        assert score >= 6.0

    def test_score_viral(self):
        engine = TopicIntelligenceEngine()
        score = engine._score_viral(
            "Why Everyone Is Wrong About AI",
            "This is controversial!",
        )
        assert score >= 6.0

    @patch.object(TopicIntelligenceEngine, "_ollama")
    @patch("src.topic_intelligence.TopicIntelligenceEngine.fetch_source_topics")
    def test_fallback_candidates(self, mock_fetch, mock_ollama):
        mock_fetch.return_value = []
        mock_ollama.return_value = (
            '[{"topic": "Fallback Topic", "category": "programming_concepts", '
            '"hook": "Hook", "angle": "Angle"}]'
        )
        engine = TopicIntelligenceEngine()
        candidates = engine._fallback_candidates()
        assert len(candidates) == 1
        assert candidates[0]["topic"] == "Fallback Topic"
