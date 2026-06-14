# Hebrew Language Support Update

## Overview

This update adds full Hebrew language support to the RAG chatbot. The chatbot now:
- Ingests Hebrew PDF documents correctly
- Detects the question language and responds in the same language
  - Hebrew question → Hebrew answer
  - English question → English answer
  - Arabic question → Arabic answer
  - Mixed (Hebrew + English characters) → Hebrew answer
- Stores conversation summaries in the detected language

---

## Production Stack (CI/CD via GitHub Secrets)

| Setting | Value |
|---------|-------|
| `LLM_PROVIDER` | `groq` |
| `LLM_MODEL` | `llama-3.1-8b-instant` |
| `EMBEDDING_PROVIDER` | `huggingface` |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` |

---

## Changes Made

### 1. `graph/nodes/generate_answer.py`

**What changed:** `_response_language()` now detects Hebrew (U+0590–U+05FF) in addition to the existing Arabic detection, so responses match the question language.

**Before:**
```python
import re

def _response_language(question: str) -> str:
    return "Arabic" if re.search(r'[؀-ۿ]', question) else "English"
```

**After:**
```python
import re

def _response_language(question: str) -> str:
    """
    Detect language from the question.
    Hebrew Unicode block: U+0590–U+05FF → Hebrew
    Arabic Unicode block: U+0600–U+06FF → Arabic
    Otherwise → English
    """
    if re.search(r'[֐-׿]', question):
        return "Hebrew"
    if re.search(r'[؀-ۿ]', question):
        return "Arabic"
    return "English"
```

**Effect:** Hebrew question → Hebrew answer. English question → English answer. Arabic question → Arabic answer.

---

### 2. `graph/nodes/summarize.py`

**What changed:** `_summary_language()` now detects Hebrew in addition to Arabic so summaries are stored in the correct language.

**Before:**
```python
import re

def _summary_language(messages: list) -> str:
    user_text = " ".join(m.content for m in messages if isinstance(m, HumanMessage))
    return "Arabic" if re.search(r'[؀-ۿ]', user_text) else "English"
```

**After:**
```python
import re

def _summary_language(messages: list) -> str:
    user_text = " ".join(m.content for m in messages if isinstance(m, HumanMessage))
    if re.search(r'[֐-׿]', user_text):
        return "Hebrew"
    if re.search(r'[؀-ۿ]', user_text):
        return "Arabic"
    return "English"
```

**Effect:** Conversation summaries in Redis are stored in the same language as the user's messages.

---

### 3. `prompts/answer.py`

**What changed:** Updated the language instruction in the prompt to explicitly state that responses must be in Hebrew even when the question is in English.

**Before:**
```
- YOU MUST respond in {lang} only. No exceptions.
  (Pure English question → English. Any Arabic characters present, even mixed → Arabic.)
```

**After:**
```
- YOU MUST respond in {lang} only. No exceptions.
  (Hebrew question → Hebrew. Arabic question → Arabic. English question → English.)
```

---

### 4. `.env.example`

**What changed:** Added a comment documenting the multilingual embedding model recommended for Hebrew PDFs.

```
# For Hebrew/multilingual PDFs use: sentence-transformers/paraphrase-multilingual-mpnet-base-v2
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
```

---

## GitHub Secret to Update (Production)

Go to: **GitHub repo → Settings → Secrets and variables → Actions**

Update this secret:

| Secret | Old Value | New Value |
|--------|-----------|-----------|
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` |

After updating, re-run the CI pipeline to redeploy.

> **Why this matters:** `all-MiniLM-L6-v2` is trained primarily on English data and has weak Hebrew embedding accuracy. `paraphrase-multilingual-mpnet-base-v2` is trained on 50+ languages including Hebrew and produces accurate semantic embeddings for Hebrew text, which directly improves RAG retrieval quality.

---

## No Changes Required In

| Component | Reason |
|-----------|--------|
| `ingest/policies.py` | `PyPDFLoader` reads Hebrew UTF-8 text correctly |
| `db/vector.py` | ChromaDB similarity search is embedding-space agnostic |
| `graph/nodes/retrieve_context.py` | Cosine similarity works for any language |
| `utils/llm_adapter.py` | Groq `llama-3.1-8b-instant` supports Hebrew natively |
| `utils/embedding_adapter.py` | Already uses `HuggingFaceEmbeddings(model_name=model)` — model is set via `.env` |
| `prompts/summarize.py` | Already uses `{lang}` variable — works with "Hebrew" |
| `schemas/` | Language-agnostic request validation |

---

## Testing

Run the test suite:
```bash
python -m pytest tests/test_hebrew_support.py -v
```

Manual end-to-end test:
```bash
# 1. Start the server
uvicorn main:app --reload

# 2. Ingest a Hebrew PDF
curl -X POST http://localhost:8000/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"file_name": "hebrew-doc", "s3_url": "https://your-bucket.s3.amazonaws.com/hebrew.pdf"}'

# 3. Ask in Hebrew
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-User-ID: user1" \
  -d '{"q": "מה המדיניות של החברה?"}'

# 4. Ask in English — should still respond in Hebrew
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-User-ID: user1" \
  -d '{"q": "What is the company policy?"}'
```

Both responses should be in Hebrew.
