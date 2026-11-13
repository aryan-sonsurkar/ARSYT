from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.content_generator import ContentGenerator, CATEGORIES


class TestContentGenerator:
    def test_categories_defined(self):
        assert len(CATEGORIES) == 6
        assert "programming_concepts" in CATEGORIES

    @patch("src.content_generator.ContentGenerator._ollama_generate")
    def test_generate_content(self, mock_ollama):
        mock_ollama.return_value = "This is a test content paragraph about coding."
        gen = ContentGenerator()
        result = gen.generate_content("Test Topic", "programming_concepts")
        assert "test content" in result.lower()
        mock_ollama.assert_called_once()

    @patch("src.content_generator.TopicIntelligenceEngine.generate_topic")
    @patch("src.content_generator.ContentGenerator.generate_content")
    def test_generate_topic_and_content(self, mock_content, mock_topic):
        mock_topic.return_value = {
            "topic": "Why Rust is Safe",
            "category": "programming_languages",
            "hook": "Did you know Rust prevents memory bugs at compile time?",
            "angle": "Explore Rust's ownership model",
            "overall_score": 8.5,
            "all_candidates": [],
        }
        mock_content.return_value = "Rust content paragraph."
        gen = ContentGenerator()
        result = gen.generate_topic_and_content()
        assert result["topic"] == "Why Rust is Safe"
        assert result["category"] == "programming_languages"
        assert result["content"] == "Rust content paragraph."
        assert result["overall_score"] == 8.5
