"""Shared settings. Secrets come from .env (never committed) or real environment variables."""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_env_file():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

SITE_URL = "https://analyst-assessment-production.up.railway.app"
API_BASE_URL = os.environ.get("API_BASE_URL", f"{SITE_URL}/api/v1").rstrip("/")
API_TOKEN = os.environ.get("API_TOKEN", "")

DB_PATH = DATA_DIR / "review.db"
