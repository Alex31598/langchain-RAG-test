# langchain-RAG-test

A single repository containing two complementary, production-minded MCP-server
projects: a **simulated telemetry MCP** with a LangGraph diagnostic agent
(Project 1), and an **enterprise RAG MCP** over NASA technical reports with
hybrid retrieval and a Ragas evaluation harness (Project 2). Both ship as
FastMCP servers, run together locally via Docker Compose, and use a `uv`
workspace monorepo.

> **Localhost only.** Every host port binds to `127.0.0.1`. Internet/LAN
> exposure is explicitly out of scope. See `tasks/PLAN.md` §6.1, §8, §9.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) ≥ 0.9.18 (workspace + dependency management)
- Docker + Docker Compose (runs Qdrant, Ollama, and both MCP servers)

## Quickstart

```bash
# 1. Configure secrets (never committed; .env is gitignored).
cp .env.example .env
# Edit .env:
#   MCP_AUTH_TOKEN      -> openssl rand -hex 32
#   NVIDIA_NIM_API_KEY  -> your NVIDIA NIM key (optional for skeleton-only runs)

# 2. Validate the compose file (requires MCP_AUTH_TOKEN to be set).
docker compose config > /dev/null

# 3. Bring the whole stack up (loopback-only host bindings).
docker compose up --build
```

After startup the two MCP servers are reachable on the loopback interface only
(see table below). Qdrant and Ollama are internal to the `mcpnet` Docker network
and have no host port binding.

To run the test suite without Docker:

```bash
uv sync --all-packages
uv run pytest
```

## Services & ports

| Service | Host binding | Internal | Notes |
|---|---|---|---|
| `telemetry-mcp` | `127.0.0.1:8100` -> `8000` | `mcpnet` | FastMCP (HTTP/SSE); bearer auth |
| `rag-mcp` | `127.0.0.1:8101` -> `8000` | `mcpnet` | FastMCP (HTTP/SSE); bearer auth; depends on qdrant + ollama |
| `qdrant` | _none_ | `mcpnet` | No host port (unauthenticated by default); debug via `docker exec` |
| `ollama` | _none_ | `mcpnet` | No host port (unauthenticated by default); debug via `docker exec` |

All four services share one internal `mcpnet` bridge network; only the two MCP
servers publish ports, and only on `127.0.0.1`. **Never** change a binding to
`0.0.0.0`; for remote demos use an SSH tunnel or an authenticated TLS reverse
proxy.

## Security

- **Bearer auth:** every MCP tool call requires `Authorization: Bearer
  <MCP_AUTH_TOKEN>`. No token → HTTP 401. `docker compose up` refuses to start
  if `MCP_AUTH_TOKEN` is unset (`:?` in `docker-compose.yml`).
- **No secrets committed:** `.env` and `*.env` are gitignored; `.env.example`
  holds placeholders only. `NVIDIA_NIM_API_KEY` is never logged or placed in
  prompts, tool descriptions, or retrieved payloads.
- **Loopback-only exposure:** all host ports bind to `127.0.0.1`; Qdrant and
  Ollama have no host ports.
- **Untrusted data handling:** telemetry sensor logs and RAG retrieved text are
  fenced as untrusted (`<observation>`/`<context>`) in LLM prompts; RAG outputs
  pass through an API-key-pattern redactor; `data/` and `evaluation/reports/`
  are gitignored and may carry injected text.
- **Egress:** container outbound is limited to NVIDIA NIM and `ntrs.nasa.gov`;
  the RAG ingestion path enforces an `ntrs.nasa.gov` allow-list, https-only,
  no cross-host redirects, and private-IP/loopback rejection (SSRF guard).

Full safety & quality gates: see `tasks/PLAN.md` §8.

## Repository layout

```
langchain-RAG-test/
├── README.md                       # this file
├── tasks/PLAN.md                   # full vision, tech stack, milestones
├── pyproject.toml                  # uv workspace root (shared dev deps)
├── uv.lock                         # committed lockfile
├── docker-compose.yml              # qdrant, ollama, telemetry-mcp, rag-mcp
├── .env.example                    # NVIDIA_NIM_API_KEY, ports, model names
├── projects/
│   ├── telemetry-mcp/              # Project 1 — simulated accelerator + agent
│   └── rag-mcp/                    # Project 2 — RAG over NASA NTRS + evaluation
└── packages/                       # (optional) shared utilities
```

The complete structure (per-module breakdown) is in `tasks/PLAN.md` §3.

## Projects

### Project 1 — Telemetry MCP & Agent (`projects/telemetry-mcp/`)
A FastMCP server exposing a simulated particle-accelerator control system
(subsystems, sensors, fault scenarios, safety-interlocked commands), plus a
LangGraph agent that autonomously diagnoses injected faults from a log stream.
Details: `tasks/PLAN.md` §4.

### Project 2 — RAG MCP & Evaluation (`projects/rag-mcp/`)
A LangChain RAG pipeline over NASA NTRS technical reports (PDFs), exposed as a
FastMCP server with hybrid retrieval (Qdrant dense + sparse/BM25 with
reciprocal-rank fusion), BGE-Reranker rescoring, and an automated Ragas
evaluation harness (synthetic test set + Faithfulness/Answer
Relevance/Context Precision scoring). Details: `tasks/PLAN.md` §5.

## Further reading

- `tasks/PLAN.md` — full vision, locked tech stack, milestone plan, security model
- `tasks/README.md` — per-task breakdown with dependencies and status
- `SECURITY_AUDIT.md` — security audit report of the scaffolding

## License

See `LICENSE`.
