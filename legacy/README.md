# Legacy Streamlit reference

`streamlit_app.py` and `.streamlit/config.toml` are retained only as a
rollback/reference artifact during the FastAPI + React cutover. They are not
imported, built, served, or included in the production dependency set. Do not
expose them as a second runtime: that UI has a separate authentication/request
path and is not covered by the FastAPI security middleware.

The supported application is started with:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

If a temporary local investigation of the old UI is unavoidable, install its
optional packages separately and run it from a disposable environment. Remove
this directory after the rollback window closes.
