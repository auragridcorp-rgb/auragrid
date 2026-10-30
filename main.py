"""Compatibility entry point: the backend lives in app/main.py.

Kept so `uvicorn main:app` (Dockerfile) and `python main.py` keep working.
"""
from app.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
