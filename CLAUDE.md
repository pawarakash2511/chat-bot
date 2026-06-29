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
AZURE_OPENAI_API_VERSION=2025-01-01-preview
AZURE_DEPLOYMENT_NAME=gpt-5-mini
EMBEDDING_PROVIDER=huggingface
EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

> **Hebrew support:** The chatbot detects the question language and responds in the same language (Hebrew → Hebrew, English → English, Arabic → Arabic). Uses HuggingFace multilingual embeddings for accurate Hebrew PDF retrieval.

> **Out-of-scope:** If no relevant documents are found (score < 0.3), the bot responds with a polite "contact Ronen Barak" message instead of hallucinating.

## Architecture

This is a **RAG-augmented stateful chatbot** backend. Standard requests go through a LangGraph state machine; streaming requests bypass the graph for token-level SSE streaming.

### Request lifecycle — Chat (`POST /api/chat`)

```
HTTP request (X-User-ID header, {q: "..."})
  → controllers/chat_controller.py
  → services/chat_service.py  →  LangGraph state machine
       ┌──────────────────────────────────────────────────┐
       │ 1. load_memory      (Redis)                      │
       │ 2. retrieve_context (ChromaDB, k=6, score≥0.3)  │
       │ 3. generate_answer  (Azure OpenAI, max_tokens=600│
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
       2. similarity_search_with_relevance_scores (k=6, threshold=0.3)
       3. build prompt with source citations
       4. llm.astream() → yield SSE tokens: data: {"token": "..."}\n\n
       5. on complete: save updated messages to Redis
       → data: {"done": true}\n\n
```

SSE format: each event is `data: <JSON>\n\n`. Token events: `{"token": "..."}`. End event: `{"done": true}`. Error: `{"error": "..."}`.

### Request lifecycle — Ingest (`POST /api/ingest`)

```
{file_name, s3_url}  →  ingest_controller.py  →  ingest_service.py
  → policies.py: download PDF → SHA-256 dedup check (Redis set)
    → chunk (500 chars, 50 overlap) → embed (HuggingFace) → store in ChromaDB
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
| `static/index.html` | Chat frontend — streaming UI, dark/light mode, suggested questions, RTL support |
| `static/admin.html` | Knowledge base management page — ingest PDFs, view indexed documents |
| `config.py` | Pydantic `Settings` — all config read from `.env` |
| `prompts/answer.py` | Answer prompt — includes out-of-scope "contact Ronen Barak" rule and citation instructions |

### Memory strategy

- **Short-term**: last 6 messages kept in `messages` list
- **Long-term**: rolling summary stored alongside messages in Redis
- **Expiry**: TTL-based (default 24 h, `REDIS_TTL_SECONDS`)
- **Per-user key**: `user_id` from `X-User-ID` header (default: `"anonymous"`)

### Retrieval strategy

- `similarity_search_with_relevance_scores(q, k=6)` — cosine similarity
- Threshold: `score >= 0.3` — below this, treated as "no relevant docs found"
- Each retrieved chunk includes `[Source: filename, Page N]` prefix for citations
- Chunk size: 500 chars, overlap 50 — balanced for 500-page Hebrew PDFs
- No token limit concerns — Azure OpenAI has 128k context window

### Multi-provider config

Switch LLM backend via `.env`:

```
LLM_PROVIDER=azure | openai | anthropic | groq

# Azure (production — recommended)
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-01-01-preview
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
| `/` or `/index.html` | Chat page — streaming responses, dark/light mode, suggested questions |
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
| `AZURE_OPENAI_API_VERSION` | `2025-01-01-preview` |
| `AZURE_DEPLOYMENT_NAME` | `gpt-5-mini` |
| `EMBEDDING_PROVIDER` | `huggingface` |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `REDIS_PASSWORD` | Leave blank (Redis has no password) |

> **Remove old secrets:** `GROQ_API_KEY`, `LLM_MODEL` are no longer needed.

See [CI_CD.md](CI_CD.md) for full setup guide and troubleshooting.

## After deploy: full cleanup required

chunk_size changed (150 → 500), so old ChromaDB vectors must be wiped:

```bash
docker exec chatbot-redis-1 redis-cli FLUSHDB
cd ~/chatbot && docker-compose down
sudo rm -rf ~/chatbot/chroma_db
docker-compose up -d
# Then re-ingest all PDFs via /admin.html
```
