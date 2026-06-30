# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Development server (auto-reload)
uvicorn main:app --reload

# Full stack (API + Redis) via Docker
docker-compose up --build

# Production server (single worker — ChromaDB SQLite requires 1 process)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Copy `.env.example` to `.env`. Minimum to run with Azure OpenAI:
```
LLM_PROVIDER=azure
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-04-01-preview
AZURE_DEPLOYMENT_NAME=gpt-5-mini
EMBEDDING_PROVIDER=huggingface
EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

> **Hebrew support:** The chatbot detects the question language and responds in the same language (Hebrew → Hebrew, English → English, Arabic → Arabic). Uses HuggingFace multilingual embeddings for accurate Hebrew PDF retrieval.

> **Out-of-scope:** If no relevant documents are found (score < 0.15), the bot responds with a polite "contact Ronen Barak" message instead of hallucinating.

> **Mobile support:** Fully responsive for iOS (safe-area insets, no zoom-on-focus), Android (back-button closes sidebar), and all screen sizes down to 320px.

## Architecture

This is a **RAG-augmented stateful chatbot** backend. Standard requests go through a LangGraph state machine; streaming requests bypass the graph for token-level SSE streaming.

### Request lifecycle — Chat (`POST /api/chat`)

```
HTTP request (X-User-ID header, {q: "..."})
  → controllers/chat_controller.py
  → services/chat_service.py  →  LangGraph state machine
       ┌──────────────────────────────────────────────────┐
       │ 1. load_memory      (Redis)                      │
       │ 2. retrieve_context (ChromaDB, k=10, score≥0.15)│
       │ 3. generate_answer  (Azure OpenAI, max_tokens=   │
       │                      4000)                       │
       │ 4. summarize        (LLM)                        │
       │ 5. store_memory     (Redis)                      │
       └──────────────────────────────────────────────────┘
  → last AIMessage content returned as JSON response
```

### Request lifecycle — Streaming Chat (`POST /api/chat/stream`)

```
HTTP request (X-User-ID header, {q: "..."})
  → controllers/chat_controller.py
  → services/chat_service.stream_conversation()
       1. load memory directly from Redis
       2. similarity_search_with_relevance_scores (k=10, threshold=0.15)
       3. build prompt with source citations
       4. llm.astream() — retry ×2 if 0 tokens returned
          → yield SSE tokens: data: {"token": "..."}\n\n
       5. if all stream attempts empty → llm.invoke() fallback
          → if still empty → simplified prompt invoke() fallback
       6. on complete: save updated messages to Redis
       → data: {"done": true}\n\n
```

SSE format: each event is `data: <JSON>\n\n`. Token events: `{"token": "..."}`. End event: `{"done": true}`. Error: `{"error": "..."}`.

### Request lifecycle — Ingest (`POST /api/ingest`)

```
{file_name, s3_url}  →  ingest_controller.py  →  ingest_service.py
  → policies.py: download PDF → SHA-256 dedup check (Redis set)
    → chunk (250 chars, 80 overlap) → embed (HuggingFace) → store in ChromaDB
    → page number stored in doc.metadata["page"] for citations
```

### Key modules

| Path | Role |
|------|------|
| `graph/builder.py` | Compiles the LangGraph DAG |
| `graph/state.py` | `State` TypedDict: `user_id`, `question`, `messages`, `docs`, `summary`, `sources` |
| `graph/nodes/` | One file per node (load_memory, retrieve_context, generate_answer, summarize, store_memory) |
| `utils/llm_adapter.py` | Factory: returns `AzureChatOpenAI` / `ChatOpenAI` / `ChatAnthropic` / `ChatGroq` based on `LLM_PROVIDER` |
| `utils/embedding_adapter.py` | Factory: returns `OpenAIEmbeddings` / `HuggingFaceEmbeddings` based on `EMBEDDING_PROVIDER` |
| `db/redis_client.py` | Singleton Redis connection |
| `db/vector.py` | Singleton ChromaDB connection — uses `chromadb.PersistentClient` (required for ChromaDB 1.x) |
| `main.py` | App entry point — mounts `StaticFiles` at `/` after API routers so `/api/*` takes priority |
| `static/index.html` | Chat frontend — streaming UI, dark/light mode, suggested questions, RTL support, mobile-responsive |
| `static/admin.html` | Knowledge base management page — ingest PDFs, view indexed documents |
| `config.py` | Pydantic `Settings` — all config read from `.env` |
| `prompts/answer.py` | Answer prompt — two-case rule: empty context → Ronen Barak; context exists → answer from it |

### Memory strategy

- **Short-term**: last 6 messages kept in `messages` list
- **Long-term**: rolling summary stored alongside messages in Redis
- **Expiry**: TTL-based (default 24 h, `REDIS_TTL_SECONDS`)
- **Per-user key**: `user_id` from `X-User-ID` header (default: `"anonymous"`)

### Retrieval strategy

- `similarity_search_with_relevance_scores(q, k=10)` — cosine similarity
- Threshold: `score >= 0.15` — below this, treated as "no relevant docs found"
- Each retrieved chunk includes `[Source: filename, Page N]` prefix for citations
- Chunk size: 250 chars, overlap 80 — one legal clause per chunk for focused embeddings
- No token limit concerns — Azure OpenAI has 128k context window

### Multi-provider config

Switch LLM backend via `.env`:

```
LLM_PROVIDER=azure | openai | anthropic | groq

# Azure (production — recommended)
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-04-01-preview
AZURE_DEPLOYMENT_NAME=gpt-5-mini

# OpenAI (alternative)
LLM_MODEL=gpt-4o-mini

# Embeddings — keep HuggingFace (free, multilingual, Hebrew-capable)
EMBEDDING_PROVIDER=huggingface
EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

### Frontend pages

| URL | Page |
|-----|------|
| `/` or `/index.html` | Chat page — streaming responses, dark/light mode, suggested questions, fully mobile-responsive |
| `/admin.html` | Knowledge base management — ingest PDFs, view document list |

## CI/CD

Two separate workflow files, CD auto-triggers when CI completes successfully.

```
.github/workflows/ci.yml   — manual trigger (workflow_dispatch)
  Install deps → docker build → push to Docker Hub (pawarakash2511/chatbot-app:<sha>)
        ↓  workflow_run: completed (auto)
.github/workflows/cd.yml
  SSH to EC2 → copy docker-compose.yml → generate .env
  → docker pull <sha> → docker-compose down/up → health check
```

### Required GitHub Secrets

| Secret | Value |
|--------|-------|
| `EC2_HOST` | EC2 public IP or hostname |
| `EC2_USERNAME` | SSH user (`ec2-user` or `ubuntu`) |
| `EC2_SSH_KEY` | PEM private key contents |
| `DOCKERHUB_USERNAME` | `pawarakash2511` |
| `DOCKERHUB_TOKEN` | Docker Hub access token |
| `LLM_PROVIDER` | `azure` |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` |
| `AZURE_DEPLOYMENT_NAME` | `gpt-5-mini` |
| `EMBEDDING_PROVIDER` | `huggingface` |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `REDIS_PASSWORD` | Leave blank (Redis has no password) |

> **Remove old secrets:** `GROQ_API_KEY`, `LLM_MODEL` are no longer needed.

See [CI_CD.md](CI_CD.md) for full setup guide and troubleshooting.

## After deploy: full cleanup required

chunk_size changed (500 → 250), so old ChromaDB vectors must be wiped and all PDFs re-ingested:

```bash
docker exec chatbot-redis-1 redis-cli FLUSHDB
cd ~/chatbot && docker-compose down
sudo rm -rf ~/chatbot/chroma_db
docker-compose up -d
# Then re-ingest all PDFs via /admin.html
```

---

## Design Decisions — Interview Q&A

This section explains **why** each parameter was chosen, not just what it is.

### Why cosine similarity instead of L2 (Euclidean)?

L2 measures absolute vector magnitude difference between two vectors. Cosine similarity measures the **angle** between vectors, ignoring magnitude. Text embeddings can have very different magnitudes depending on document length — a short clause and a long paragraph about the same topic will score near 1.0 with cosine (same direction) but far apart with L2 (different magnitudes). Cosine is the industry standard for semantic text search for this reason.

**Practical consequence here:** Before switching to cosine, ChromaDB returned L2 distances of -4 to -11. These are not similarity scores — the code was misreading them as cosine values and everything was filtered out. After switching to `hnsw:space=cosine` + `normalize_embeddings=True`, scores correctly range 0.0–1.0.

### Why `normalize_embeddings=True` in the HuggingFace adapter?

When vectors are L2-normalized (unit length), `dot_product(A, B) == cosine_similarity(A, B)`. ChromaDB's HNSW index with `hnsw:space=cosine` expects unit vectors. Without normalization, the dot product differs from cosine and scores are wrong. This is a one-line fix that must be set at embedding time — it cannot be applied retroactively to already-stored vectors (requires re-ingestion).

### Why threshold 0.15 (started at 0.3, then 0.2)?

- **0.3 (original):** Too strict. Hebrew questions phrased differently from the stored text scored 0.17–0.19 and were filtered out, triggering the Ronen Barak contact message incorrectly.
- **0.2 (interim):** Better, but after chunk_size was reduced to 250 chars, scores improved to 0.19–0.81 and 0.15 was sufficient.
- **0.15 (current):** Still well above random noise (scores cluster near 0.0 for unrelated content). The prompt's "context only" rule prevents hallucination — worst case is the model correctly says it can't find an answer in context and routes to Ronen Barak.

The safe lower bound for this multilingual model: anything above ~0.10 is a meaningful match; 0.15 gives a comfortable margin.

### Why k=10 (was k=6)?

Higher k = better recall at negligible cost. With 500-page PDFs split into thousands of 250-char chunks, the most relevant chunk might rank 7th or 8th by cosine score. Fetching 10 candidates instead of 6 costs ~zero extra latency (ChromaDB is local, no network call) and significantly reduces the chance of missing the right chunk. All 10 are then filtered by the 0.15 threshold, so irrelevant results don't reach the LLM.

### Why chunk size 250 chars (was 500)?

Each chunk's embedding is the **average** of all word vectors in it. A 500-char chunk typically covers 3–4 distinct legal clauses — the embedding becomes a blend of all of them and matches no single clause well. Measured scores: 0.09–0.12 across all queries.

A 250-char chunk covers roughly one clause or one fact. Its embedding strongly represents that specific content. After switching, measured scores: 0.19–0.81 depending on query phrasing.

**Overlap 80 (32%):** Ensures no single fact gets cut exactly at a chunk boundary. A fact split across two chunks still has one chunk that contains it fully.

**Re-ingestion required:** Changing chunk size changes all embeddings, so the ChromaDB collection must be wiped and all PDFs re-ingested after this change.

### Why HuggingFace `paraphrase-multilingual-MiniLM-L12-v2`?

| Property | Why it matters here |
|----------|---------------------|
| **Free** | Zero per-embedding API cost (OpenAI charges ~$0.02/1M tokens) |
| **Multilingual** | Trained on 50+ languages — same model handles Hebrew, Arabic, English equally well |
| **MiniLM-L12** | 12 transformer layers — small enough to run CPU-only on EC2 t3.medium without GPU |
| **paraphrase variant** | Optimized for semantic similarity, not keyword overlap — finds "recognized pension" even when doc says "קצבה מוכרת" |
| **Warmed at startup** | `main.py` calls the embedding adapter on startup to avoid cold-start latency on first query |

### Why Azure OpenAI instead of Groq?

Groq's free tier has a **6000 TPM (tokens per minute)** limit. In production with multiple concurrent users, each conversation using ~1000 tokens, this limit is hit within seconds. Azure OpenAI provides significantly higher rate limits, enterprise SLA, GDPR compliance for EU/Israeli data, and data residency options. Cost is also predictable (pay-per-token vs. hit rate limits unpredictably).

### Why `max_tokens=4000` (was 600)?

`gpt-5-mini` on Azure is an **o-series reasoning model** (like o4-mini). These models perform internal "chain-of-thought" reasoning before generating visible output. Crucially, **`max_tokens` is the total budget for reasoning + visible output combined**.

With `max_tokens=600`: the model spent 500+ tokens on internal reasoning, leaving 0 for visible output. Result: HTTP 200, `content=""`, `refusal=None`. This is NOT a content filter (refusal would be non-null) — it's token budget exhaustion.

With `max_tokens=4000`: ~2000–3000 for reasoning + 1000–2000 for visible output. Confirmed by real logs showing answers appearing consistently after this change.

**Diagnostic:** `logger.info("Invoke response: content=%r, kwargs=%r", response.content, response.additional_kwargs)` — if `content=''` and `refusal=None`, it's token budget. If `refusal` is non-null, it's a content filter.

### Why stream retry (2 attempts) before invoke() fallback?

`gpt-5-mini` intermittently returns 0 visible tokens from `astream()` — the reasoning model occasionally burns its entire budget on internal thinking even with `max_tokens=4000`. The same query succeeds ~95% of the time on immediate retry (the model's reasoning path differs per attempt).

Fallback chain:
1. `astream()` — try ×2 (user sees typing indicator the whole time)
2. `llm.invoke()` — synchronous, same prompt
3. `llm.invoke()` — simplified prompt (strips `[Source/Page]` prefixes from context)

Each fallback is logged so production issues are diagnosable without reproducing them.

### Why SSE (Server-Sent Events) instead of WebSockets?

SSE is **one-directional** (server → client), which is all that's needed here. Benefits over WebSockets:
- No protocol upgrade — plain HTTP/1.1, works through all proxies and load balancers
- Built-in browser reconnect (EventSource auto-reconnects on disconnect)
- FastAPI's `StreamingResponse` handles SSE natively — no extra library
- Client is simpler: `new EventSource(url)` vs. managing a WebSocket lifecycle

WebSockets add bidirectional complexity (connection state, heartbeats, reconnect logic) that provides no benefit when only the server needs to send data.

### Why LangGraph for the non-streaming path?

LangGraph provides a compiled state machine where each step (load_memory → retrieve_context → generate_answer → summarize → store_memory) is an isolated, independently testable node. The graph makes the flow explicit and debuggable — state is typed via `TypedDict` and each node sees exactly the fields it needs.

**Why streaming bypasses LangGraph:** LangGraph executes nodes sequentially and returns a final state. It cannot yield tokens mid-execution. For SSE, `astream()` must be called directly so tokens flow to the client immediately. The streaming path reimplements the same logic inline.

### Why two-tier memory (last 6 messages + rolling summary)?

- **Last 6 messages (short-term):** Exact text provides the model with precise recent context — "as I said before" references, follow-up questions, clarifications.
- **Rolling LLM summary (long-term):** Captures conversation history beyond 6 messages without blowing up the prompt. Without this, a user's name or company mentioned 10 messages ago would be forgotten.

**Redis TTL (24h):** Memory expires automatically without any cleanup job. Each user's conversation is stored under their `X-User-ID` header value.

### Why the two-case prompt rule for out-of-scope detection?

**Original rule:** "If context is empty OR does not contain a relevant answer → Ronen Barak"

The "does not contain a relevant answer" clause was too broad. The LLM incorrectly decided Hebrew context "didn't contain an answer" for Hebrew questions even when it clearly did — likely because the model was uncertain about Hebrew language matching.

**Current rule (two-case):**
1. If context is empty/no docs found → Ronen Barak (always)
2. If context has content → answer from it; if truly not addressed, use Ronen Barak — but NO custom refusal messages

This prevents both hallucination (rule 2 forbids general knowledge) and over-eager contact messages (rule 2 requires using available context first).

### Why `viewport-fit=cover` + `env(safe-area-inset-*)` on mobile?

iPhones have a home indicator bar at the bottom and a notch/Dynamic Island at the top. By default, Safari keeps content within the "safe area" (away from these elements), which leaves an awkward gap. `viewport-fit=cover` extends the viewport edge-to-edge. `env(safe-area-inset-bottom)` then gives the exact pixel height to add as padding so the send button isn't obscured by the home bar. This is the correct iOS pattern — not a workaround.

**Android:** `history.pushState` on sidebar open + `popstate` listener intercepts the Android back button to close the sidebar instead of navigating away from the page.
