"""WSGI entry point.

Production:   gunicorn wsgi:app -c gunicorn.conf.py
Development:  python run.py
"""
from app import create_app

app = create_app()
