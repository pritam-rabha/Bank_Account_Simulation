"""Gunicorn settings:  gunicorn wsgi:app -c gunicorn.conf.py

JSON storage is protected by a thread lock plus an OS file lock, so several
workers are safe on ONE machine. A single worker with threads is the default
because it is simplest and enough for a classroom demo.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = 30
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
