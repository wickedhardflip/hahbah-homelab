"""Entry point for uvicorn: builds the app from environment settings."""
import logging
import os

from .main import create_app

os.umask(0o077)   # the database, sessions and queued emails are owner-only (the collector runs as the same uid)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
