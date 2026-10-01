# Web V2 Preview

This branch is an isolated, conservative preview of a future PortScope web interface.

## Safety boundary

- The existing `main` branch and Streamlit deployment are untouched.
- The Web V2 preview uses the existing sanitized `PublicDemoRepository`.
- The backend exposes read-only endpoints only.
- No collection, upload, AI execution, report generation, admin operation, or database write endpoint is exposed.
- The existing public demo database remains opened by `public_demo_store.py` in SQLite read-only/query-only mode.

## Files added

- `web_v2.py` — FastAPI read-only API + static frontend host.
- `web/index.html` — Web V2 shell.
- `web/style.css` — responsive enterprise-style UI.
- `web/app.js` — browser-side read-only data binding.
- `requirements-web-v2.txt` — additional preview dependencies.

## Local preview

From the repository root:

```bash
pip install -r requirements.txt
pip install -r requirements-web-v2.txt
uvicorn web_v2:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

API documentation is available locally at:

```text
http://127.0.0.1:8000/api/docs
```

## Promotion rule

Do not merge this branch into `main` and do not replace the current resume link until the preview has been visually and functionally reviewed.
