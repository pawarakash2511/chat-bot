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

_RELEVANCE_THRESHOLD = 0.2


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
    import asyncio
    full_response = ""
    messages, summary = [], ""
    lang = "English"
    relevant = []
    try:
        messages, summary = _load_redis_memory(user_id)

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: get_vectorstore().similarity_search_with_relevance_scores(q, k=6),
        )
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
        logger.info("LLM stream starting for user %s", user_id)
        token_count = 0
        async for chunk in llm.astream(prompt):
            token = chunk.content
            if token:
                token_count += 1
                full_response += token
                yield f"data: {json.dumps({'token': token})}\n\n"
        logger.info("LLM stream done for user %s: %d tokens", user_id, token_count)

        if not full_response:
            logger.warning("0-token stream for user %s, retrying with invoke()", user_id)
            response = await loop.run_in_executor(None, lambda: llm.invoke(prompt))
            logger.info("Invoke response: content=%r, kwargs=%r", response.content, response.additional_kwargs)
            full_response = response.content or ""
            if full_response:
                yield f"data: {json.dumps({'token': full_response})}\n\n"
                logger.info("Invoke fallback succeeded for user %s: %d chars", user_id, len(full_response))
            else:
                logger.warning("Invoke fallback empty for user %s, retrying with simplified prompt", user_id)
                simple_context = "\n\n".join(doc.page_content for doc, _ in relevant)
                simple_prompt = build_answer_prompt(
                    summary=summary,
                    history=history,
                    docs=simple_context,
                    question=q,
                    lang=lang,
                )
                response2 = await loop.run_in_executor(None, lambda: llm.invoke(simple_prompt))
                logger.info("Simple prompt invoke response: content=%r, kwargs=%r", response2.content, response2.additional_kwargs)
                full_response = response2.content or ""
                if full_response:
                    yield f"data: {json.dumps({'token': full_response})}\n\n"
                    logger.info("Simple prompt fallback succeeded for user %s: %d chars", user_id, len(full_response))
                else:
                    logger.error("All fallbacks returned empty for user %s", user_id)

    except Exception:
        logger.exception("Streaming error for user %s", user_id)
        yield f"data: {json.dumps({'error': 'An error occurred during response generation.'})}\n\n"
    finally:
        yield f"data: {json.dumps({'done': True})}\n\n"

    updated_messages = messages[-6:] + [
        HumanMessage(content=q),
        AIMessage(content=full_response),
    ]
    _save_redis_memory(user_id, updated_messages, summary)
    logger.info("Streamed answer for user %s (lang=%s, docs=%d)", user_id, lang, len(relevant))
