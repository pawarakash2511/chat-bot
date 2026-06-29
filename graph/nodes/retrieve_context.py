import logging

from db.vector import get_vectorstore

logger = logging.getLogger(__name__)

_RELEVANCE_THRESHOLD = 0.3


def retrieve_context(state):
    question = state["question"]
    results = get_vectorstore().similarity_search_with_relevance_scores(question, k=6)

    relevant = [(doc, score) for doc, score in results if score >= _RELEVANCE_THRESHOLD]
    logger.info("Retrieved %d/%d docs above threshold for query", len(relevant), len(results))

    chunks = []
    sources = []
    for doc, _score in relevant:
        src = doc.metadata.get("source_file", "document")
        page = doc.metadata.get("page", 0) + 1
        chunks.append(f"[Source: {src}, Page {page}]\n{doc.page_content}")
        sources.append(f"{src}, Page {page}")

    context = "\n\n".join(chunks)
    return {"docs": context, "sources": sources}
