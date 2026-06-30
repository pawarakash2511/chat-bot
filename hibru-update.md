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
| `LLM_PROVIDER` | `azure` |
| `AZURE_DEPLOYMENT_NAME` | `gpt-5-mini` |
| `EMBEDDING_PROVIDER` | `huggingface` |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |

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
# For Hebrew/multilingual PDFs use: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
```

---

## GitHub Secret to Update (Production)

Go to: **GitHub repo → Settings → Secrets and variables → Actions**

Update this secret:

| Secret | Old Value | New Value |
|--------|-----------|-----------|
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |

After updating, re-run the CI pipeline to redeploy.

> **Why this matters:** `all-MiniLM-L6-v2` is trained primarily on English data and has weak Hebrew embedding accuracy. `paraphrase-multilingual-MiniLM-L12-v2` is trained on 50+ languages including Hebrew and produces accurate semantic embeddings for Hebrew text, which directly improves RAG retrieval quality. It is also 120MB vs the larger mpnet variant (420MB), making it suitable for CPU-only EC2 instances.

---

### 5. `ingest/policies.py` — Hebrew PDF Text Extraction Fix

**What changed:** `_clean_text()` now detects and fixes two types of broken Hebrew PDF text before storing in ChromaDB.

Two new helper functions added:

```python
def _fix_hebrew_visual_order(text):
    # Reverses each Hebrew line character-by-character
    # Fixes PDFs where text is stored in visual/display order
    # e.g. 'לארשיב סמה יללכ' → 'כללי המס בישראל'

def _fix_hebrew_encoding(text):
    # Re-encodes Latin-1 bytes as CP1255 (Windows Hebrew)
    # Fixes PDFs where Hebrew was mis-decoded as Latin-1
    # e.g. 'îàú òå"ã' → 'מאת עו"ד'
```

Auto-detection logic in `_clean_text()`:
- Contains Hebrew Unicode chars → apply visual order fix
- Contains Latin special chars pattern (`îàú` etc.) → apply encoding fix
- Otherwise → unchanged (English PDFs unaffected)

---

## No Changes Required In

| Component | Reason |
|-----------|--------|
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

---

## Troubleshooting

### Health check failed after 10 attempts on first deploy
**Cause:** Multilingual model `paraphrase-multilingual-mpnet-base-v2` (420MB) too large for 2GB EC2 — workers dying during model download due to OOM.
**Fix:** Switch to smaller model in GitHub Secret:
```
EMBEDDING_MODEL = sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```
Then redeploy. This model is 120MB, needs ~300MB RAM — fits comfortably on 2GB EC2.

---

### Hebrew PDF retrieval returns reversed words (e.g. `לארשיב ליגרה`)
**Cause:** One or more of:
1. Old Docker image running — `ingest/policies.py` fix not yet deployed
2. ChromaDB still has old reversed-text vectors from previous ingest
3. Redis still holds the file hash → re-ingest silently skipped as duplicate

**Fix (run in order):**
```bash
# 1. Push code and trigger CI/CD first — ensure new image is deployed
# 2. Clear Redis file hash registry
docker exec chatbot-redis-1 redis-cli DEL ingested_file_hashes

# 3. Clear ChromaDB vectors
cd ~/chatbot
docker-compose down
sudo rm -rf ~/chatbot/chroma_db
docker-compose up -d

# 4. Re-ingest the Hebrew PDF via UI or API
```

---

### "Already ingested" message even after clearing ChromaDB
**Cause:** ChromaDB was cleared but Redis `ingested_file_hashes` set still holds the SHA-256 hash of the PDF.
**Fix:**
```bash
docker exec chatbot-redis-1 redis-cli DEL ingested_file_hashes
```
Then re-ingest.

---

### Two Hebrew PDF encoding types — both now handled automatically

| Type | Symptom | Root Cause | Auto-Fix |
|------|---------|-----------|----------|
| Visual order | Words readable but in wrong order | PDF stores RTL text in display order | `_fix_hebrew_visual_order()` |
| CP1255 mis-decode | Garbled chars like `îàú òå"ã` | Hebrew CP1255 bytes decoded as Latin-1 | `_fix_hebrew_encoding()` |
