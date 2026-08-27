"""Configuration loader for Facebook Scraper Pipeline."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Project root
BASE_DIR = Path(__file__).parent.resolve()

# ─── Apify ──────────────────────────────────────────────
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# ─── Facebook ───────────────────────────────────────────
FB_COOKIES_PATH = os.getenv("FB_COOKIES_PATH", str(BASE_DIR / "fb_cookies.txt"))

# ─── Proxy (optional) ────────────────────────────────────
DATAIMPULSE_PROXY = os.getenv("DATAIMPULSE_PROXY", "")

# ─── Output ──────────────────────────────────────────────
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging ────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─── Apify Actor IDs ────────────────────────────────────
SEARCH_ACTOR = "danek/facebook-search-ppr"
COMMENTS_ACTOR = "apify/facebook-comments-scraper"

# ─── Defaults ────────────────────────────────────────────
DEFAULT_MAX_POSTS = 20
DEFAULT_MAX_COMMENTS = 150
