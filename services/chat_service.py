import json
import logging
import re
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage

from db.redis_client import get_redis
from db.vector import get_vectorstore
from graph.builder import build_graph
from prompts.answer import build_answer_prompt
from utils.llm_adapter import get_llm

logger = logging.getLogger(__name__)

graph = build_graph()

_RELEVANCE_THRESHOLD = 0.1


def conversation(user_id: str, q: str) -> str:
    result = graph.invoke({"user_id": user_id, "question": q})
    return result["messages"][-1].content


def _detect_language(text: str) -> str:
    if re.search(r'[֐-׿]', text):
        return "Hebrew"
    if re.search(r'[؀-ۿ]', text):
        return "Arabic"
    return "English"


def _load_redis_memory(user_id: str) -> tuple[list, str]:
    data = get_redis().get(user_id)
    if not data:
        return [], ""
    parsed = json.loads(data)
    messages = []
    for m in parsed.get("messages", []):
        if m["role"] == "user":
            messages.append(HumanMessage(content=m["content"]))
        else:
            messages.append(AIMessage(content=m["content"]))
    return messages, parsed.get("summary", "")


def _save_redis_memory(user_id: str, messages: list, summary: str) -> None:
    from config import get_settings
    serialized = []
    for m in messages:
        if isinstance(m, HumanMessage):
            serialized.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            serialized.append({"role": "ai", "content": m.content})
    ttl = get_settings().redis_ttl_seconds
    get_redis().set(user_id, json.dumps({"summary": summary, "messages": serialized}), ex=ttl)


async def stream_conversation(user_id: str, q: str) -> AsyncGenerator[str, None]:
    """Streams answer tokens as SSE events, bypassing LangGraph for token-level streaming."""
    messages, summary = _load_redis_memory(user_id)

    results = get_vectorstore().similarity_search_with_relevance_scores(q, k=6)
    for doc, score in results:
        logger.info("Doc score=%.4f source=%s", score, doc.metadata.get("source_file", "?"))
    relevant = [(doc, score) for doc, score in results if score >= _RELEVANCE_THRESHOLD]

    chunks = []
    for doc, _score in relevant:
        src = doc.metadata.get("source_file", "document")
        page = doc.metadata.get("page", 0) + 1
        chunks.append(f"[Source: {src}, Page {page}]\n{doc.page_content}")
    context = "\n\n".join(chunks)

    lang = _detect_language(q)
    recent = messages[-6:]
    history = "\n".join(
        f"{'User' if isinstance(m, HumanMessage) else 'AI'}: {m.content}"
        for m in recent
    )

    prompt = build_answer_prompt(
        summary=summary,
        history=history,
        docs=context,
        question=q,
        lang=lang,
    )

    llm = get_llm(temperature=0, max_tokens=600)
    full_response = ""
    try:
        async for chunk in llm.astream(prompt):
            token = chunk.content
            if token:
                full_response += token
                yield f"data: {json.dumps({'token': token})}\n\n"
    except Exception as e:
        logger.exception("Streaming error for user %s", user_id)
        yield f"data: {json.dumps({'error': 'An error occurred during response generation.'})}\n\n"
        return

    yield f"data: {json.dumps({'done': True})}\n\n"

    updated_messages = messages[-6:] + [
        HumanMessage(content=q),
        AIMessage(content=full_response),
    ]
    _save_redis_memory(user_id, updated_messages, summary)
    logger.info("Streamed answer for user %s (lang=%s, docs=%d)", user_id, lang, len(relevant))
