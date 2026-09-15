# PortScope — Evidence-Traceable Public-Information Intelligence Workbench

[![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](tests/)
[![Lint](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)](pyproject.toml)
[![License](https://img.shields.io/badge/license-source--available-lightgrey)](LICENSE)

> Chinese documentation (authoritative, far more detailed): [README.md](README.md)

A single-operator, local-first workbench that collects, verifies and reports on **public**
maritime and port information for the Qingdao region. It is not an official system of any
port, maritime, weather or government authority, and it does not resell third-party content.

**The interesting problem here is not crawling or RAG — it is refusing to state anything
that cannot be traced back to specific characters in a specific version of a source document.**

---

## What is actually verified today

These numbers are generated from the live SQLite database by
`scripts/sync_readme_status.py`, not written by hand. Current values live in the
[Chinese README](README.md) status block and in [CURRENT_STATE_AUDIT.md](CURRENT_STATE_AUDIT.md).

| | Status |
|---|---|
| Automated pipeline (crawl → extract → evidence-bind → index) | **Working**, on 1 stable source |
| Test suite | **Green**, all network access mocked (exact count in the auto-generated status block) |
| Independent human review of events | **0** — the gate has never been opened |
| Events eligible for customer delivery | **0** — blocked by the line above |
| Paying users | **0** — this is a controlled internal trial, not a product |

The zeros are deliberate and load-bearing. Customer-delivery eligibility requires
independent human review with a recorded reviewer name, timestamp and method; automated
and agent-produced labels are explicitly excluded from the human gold standard. One
acceptance report was **retroactively invalidated** when it turned out 15 labels had come
from an agent rather than a person — the invalidation notice is still at the top of
`qa/real_acceptance_report.md`.

## Three design constraints

**1. Gates are deterministic code, never model judgement.**
robots/terms, SSRF, domain allowlist, body-text quality, licence rights and review
eligibility are all program rules. The LLM works *inside* those gates: it cannot enable a
source, self-approve an event, or override a deterministic score. Tool calls are restricted
to a registry of 27 Pydantic-validated schemas with `extra=forbid`; unregistered tools are
rejected outright.

**2. Every claim maps to a character range in a source document.**
`evidence_binding.py` records `document_id`, document version, `content_hash` and
start/end character offsets for each quote. Only whitespace normalisation is tolerated —
semantic paraphrase cannot masquerade as a verbatim quote. When a document changes, old
citations are invalidated automatically. Unlocatable evidence cannot enter an executive
summary or a customer deliverable.

**3. Automated metrics and the independent human gold standard are reported separately.**
`ai_assistant`, `codex_agent` and `automatic_rule` labels never count toward human accuracy.
Where human review has not happened, the report prints "pending human verification"
rather than an accuracy figure.

## Security posture

- **SSRF**: `validate_url()` in [web_extractor.py](web_extractor.py) rejects non-HTTP(S)
  schemes, credentials embedded in URLs, localhost / `.internal` / `.local` hosts, and any
  literal or DNS-resolved address that is not globally routable — cloud metadata endpoints
  included. `ContentFetcher` re-runs the whole check **on every redirect hop**, not only on
  the first URL.
- **Politeness**: identifiable `PortScope` user agent, minimum 2s per-domain interval,
  bounded timeouts / redirects / response size, exponential backoff, and conditional
  requests (`If-None-Match` / `If-Modified-Since`). No sitemap scanning, no browser
  automation, no CAPTCHA bypass, no unattended scheduled runs.
- **Prompt injection**: page body text is untrusted input. The system prompt requires the
  model to treat embedded instructions, role prompts and "ignore previous instructions" as
  ordinary text, and forbids following links, reading local files, or adding facts absent
  from retrieved evidence.
- **Secrets**: API keys are written atomically to `.env` only — never to SQLite,
  conversations, reports or logs; the UI shows `sk-****1234`. `security_audit.py` scans
  source, reports, logs and release archives for leaked secrets without ever printing a
  candidate value.

## Architecture

```text
allowlisted sources
  → robots / terms gate → SSRF + allowlist gate (re-checked per redirect)
  → incremental discovery (API / RSS / HTML listing)
  → fetch and clean (HTML) or parse (text-based PDF)
  → canonical-URL and content_hash dedup → raw archive + document version chain
  → body-quality gate (mojibake, template noise, missing date, low density)
  → LLM structured extraction (Pydantic strict) → verbatim evidence binding
  → SQLite FTS5 + Chroma/BGE-small-zh hybrid retrieval with citation enforcement
  → independent human review
  → internal-research eligibility → customer-delivery eligibility + immutable
    delivery snapshot (SHA-256)
```

A rendered diagram is in the Chinese README.

| Layer | Files |
|---|---|
| Crawling and safety | `crawler/`, `web_extractor.py` |
| Storage and versioning | `platform_db.py`, `document_processor.py`, `operation_store.py` |
| Quality gates | `document_quality.py`, `data_validator.py` |
| LLM extraction | `deepseek_service.py`, `event_pipeline.py` |
| Evidence and review | `evidence_binding.py`, `review_service.py`, `qa_promotion.py` |
| Retrieval | `rag/` (FTS5 + Chroma + hybrid + citation builder) |
| Agent tooling | `agent/` (registry, schemas, executor, approval gates) |
| Reporting | `platform_report.py`, `commercial_report.py` |
| UI | `app.py`, `ui_*.py` (Streamlit) |
| Release and audit | `package_release.py`, `security_audit.py`, `state_audit.py` |

## Running it

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

`requirements.txt` is the minimal runtime set and deliberately omits the local vector
stack. Without it, SQLite FTS5 keyword search, the whole test suite and the public demo
all still work; chunks simply stay marked "pending vectorisation". Add semantic search
with `pip install -r requirements-rag.txt` (pulls in torch, ~2GB). The Windows desktop
build installs pinned versions from `requirements-lock.txt` instead.

Read-only public demo mode — no crawling, no uploads, no AI calls, no exports:

```bash
PUBLIC_DEMO_MODE=true streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Tests (no network access; every HTTP and LLM call is mocked):

```bash
python -m pytest -q
```

CI runs the full suite on Windows, ruff on Linux, and separately validates the Linux
public-demo deployment path (`deployment_check.py`, public-safety scan, secret audit).
One test re-imports `app`, `ui_public_demo` and `rag/*` in a subprocess with `chromadb`,
`sentence-transformers` and `torch` blocked, so the cloud deployment path cannot silently
acquire a heavyweight dependency.

## Known limitations

Listed deliberately rather than hidden — the length of this list is the honest measure of
maturity:

- One source is stably productive (Shandong MSA). Others are blocked by JavaScript
  rendering, an incomplete local TLS chain, DOC/PPTX-only attachments, or timeouts.
- No OCR, no scanned or encrypted PDFs, no complex table reconstruction.
- No independent human review has been completed, so no accuracy figure is claimed and no
  event is cleared for customer delivery.
- No commercial redistribution rights have been obtained for any external source.
- Windows-first: launcher, environment repair and diagnostics are `.bat` based. The public
  demo path itself is platform-neutral.

## Licence

Source-available for evaluation and portfolio review — see [LICENSE](LICENSE). The licence
covers this repository's own code only and grants no rights over any third-party source
content; see `config/source_license_evidence.json` and `THIRD_PARTY_LICENSES.md`.
