"""
Tests for Hebrew language support.
Language is detected from the question/messages and matched in the response.
Hebrew → Hebrew, English → English, Arabic → Arabic.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import HumanMessage, AIMessage

from graph.nodes.generate_answer import _response_language
from graph.nodes.summarize import _summary_language
from prompts.answer import build_answer_prompt
from prompts.summarize import build_summarize_prompt


# ---------------------------------------------------------------------------
# _response_language — question language detection
# ---------------------------------------------------------------------------

def test_response_language_english():
    assert _response_language("What is the company policy?") == "English"


def test_response_language_hebrew():
    assert _response_language("מה המדיניות של החברה?") == "Hebrew"


def test_response_language_arabic():
    assert _response_language("ما هي سياسة الشركة؟") == "Arabic"


def test_response_language_empty():
    assert _response_language("") == "English"


def test_response_language_hebrew_mixed_with_english():
    # Hebrew characters present → Hebrew
    assert _response_language("What is מדיניות?") == "Hebrew"


# ---------------------------------------------------------------------------
# _summary_language — message history language detection
# ---------------------------------------------------------------------------

def test_summary_language_english_messages():
    messages = [
        HumanMessage(content="What is the refund policy?"),
        AIMessage(content="Returns are allowed within 30 days."),
    ]
    assert _summary_language(messages) == "English"


def test_summary_language_hebrew_messages():
    messages = [
        HumanMessage(content="מה מדיניות ההחזרים?"),
        AIMessage(content="ניתן להחזיר מוצרים תוך 30 יום."),
    ]
    assert _summary_language(messages) == "Hebrew"


def test_summary_language_arabic_messages():
    messages = [
        HumanMessage(content="ما هي سياسة الاسترداد؟"),
        AIMessage(content="يمكن إرجاع المنتجات خلال 30 يومًا."),
    ]
    assert _summary_language(messages) == "Arabic"


def test_summary_language_empty_messages():
    assert _summary_language([]) == "English"


# ---------------------------------------------------------------------------
# Prompt template — lang variable flows in correctly
# ---------------------------------------------------------------------------

def test_answer_prompt_hebrew_instruction():
    prompt = build_answer_prompt(
        summary="", history="", docs="Some content.",
        question="מה המדיניות?", lang="Hebrew",
    )
    assert "Hebrew" in prompt


def test_answer_prompt_english_instruction():
    prompt = build_answer_prompt(
        summary="", history="", docs="Some content.",
        question="What is the policy?", lang="English",
    )
    assert "English" in prompt


def test_answer_prompt_no_docs_fallback():
    prompt = build_answer_prompt(
        summary="", history="", docs="",
        question="What is the policy?", lang="English",
    )
    assert "(no relevant documents found)" in prompt


def test_summarize_prompt_hebrew():
    prompt = build_summarize_prompt(text="User: מה המדיניות?\nAI: כך וכך", lang="Hebrew")
    assert "Hebrew" in prompt


def test_summarize_prompt_english():
    prompt = build_summarize_prompt(text="User: What is the policy?\nAI: X", lang="English")
    assert "English" in prompt


# ---------------------------------------------------------------------------
# End-to-end: detection → prompt
# ---------------------------------------------------------------------------

def test_hebrew_question_flows_to_hebrew_prompt():
    lang = _response_language("מה המדיניות של החברה?")
    assert lang == "Hebrew"
    prompt = build_answer_prompt(summary="", history="", docs="content", question="מה המדיניות?", lang=lang)
    assert "Hebrew" in prompt


def test_english_question_flows_to_english_prompt():
    lang = _response_language("What is the policy?")
    assert lang == "English"
    prompt = build_answer_prompt(summary="", history="", docs="content", question="What is the policy?", lang=lang)
    assert "English" in prompt


def test_hebrew_messages_flow_to_hebrew_summary():
    messages = [HumanMessage(content="מה המדיניות?")]
    lang = _summary_language(messages)
    assert lang == "Hebrew"
    prompt = build_summarize_prompt(text="User: מה המדיניות?", lang=lang)
    assert "Hebrew" in prompt
