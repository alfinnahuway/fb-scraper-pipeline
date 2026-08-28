"""
Facebook Scraper Pipeline — Main Script
========================================
Hybrid approach:
  1. Apify danek actor for keyword search (~$0.05/run)
  2. Native Selenium + cookies for comment scraping ($0)

Features:
  - Comment sort: top (by engagement) or recent (chronological)
  - Comment depth: configurable max comments per post
  - Reply expansion: auto-click "View X replies" to get sub-comments
  - Risk detection: captures edge/angry comments (not just top-voted)

Usage:
  python fb_pipeline.py --keyword "DPR RI" --max-posts 5 --max-comments 100
  python fb_pipeline.py --keyword "DPR RI" --comment-sort recent --max-comments 200
  python fb_pipeline.py --keyword "Pilpres" --max-posts 20 --max-comments 100 --output ./output/pilpres.json
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient

from config import (
    APIFY_API_TOKEN,
    FB_COOKIES_PATH,
    DATAIMPULSE_PROXY,
    DEFAULT_MAX_POSTS,
    DEFAULT_MAX_COMMENTS,
    OUTPUT_DIR,
    SEARCH_ACTOR,
    LOG_LEVEL,
)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fb-pipeline")

# Suppress noisy seleniumwire logs
logging.getLogger("seleniumwire").setLevel(logging.WARNING)


# ─── Step 1: Search posts via Apify danek actor ──────────────────────────────

def search_posts(keyword: str, max_posts: int, since: str = None, until: str = None) -> list[dict]:
    """Search Facebook posts using danek/facebook-search-ppr actor."""
    if not APIFY_API_TOKEN:
        log.error("APIFY_API_TOKEN not set. Check .env file.")
        sys.exit(1)

    client = ApifyClient(APIFY_API_TOKEN)
    log.info(f"Searching Facebook for '{keyword}' (max {max_posts} posts)...")

    run_input = {
        "query": keyword,
        "search_type": "posts",
        "max_posts": max_posts,
    }
    if since:
        run_input["start_date"] = since
    if until:
        run_input["end_date"] = until

    run = client.actor(SEARCH_ACTOR).call(run_input=run_input)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    log.info(f"  Found {len(items)} posts from danek actor")

    posts = []
    for item in items:
        author_obj = item.get("author") or {}
        author_name = author_obj.get("name", "") if isinstance(author_obj, dict) else str(author_obj)
        url = item.get("url", "")
        page_slug = _extract_page_slug(url)

        post = {
            "post_id": str(item.get("post_id", "")),
            "pfbid_url": url,
            "numeric_url": "",
            "author": author_name,
            "author_id": str(author_obj.get("id", "")) if isinstance(author_obj, dict) else "",
            "page_slug": page_slug,
            "message": item.get("message", ""),
            "timestamp": item.get("timestamp"),
            "comments_count": item.get("comments_count", 0),
            "reactions_count": item.get("reactions_count", 0),
            "reshare_count": item.get("reshare_count", 0),
            "post_type": item.get("type", ""),
            "media": item.get("media", []),
        }

        if post["post_id"] and page_slug:
            post["numeric_url"] = f"https://www.facebook.com/{page_slug}/posts/{post['post_id']}"

        posts.append(post)

    return posts


def _extract_page_slug(url: str) -> str:
    """Extract page slug from Facebook URL."""
    match = re.search(r"facebook\.com/([^/]+)/posts/", url)
    if match:
        return match.group(1)
    match = re.search(r"facebook\.com/([^/]+)/(?:videos|reel)/", url)
    if match:
        return match.group(1)
    match = re.search(r"facebook\.com/groups/([^/]+)/", url)
    if match:
        return match.group(1)
    return ""


# ─── Step 2: Scrape comments via Native Selenium ─────────────────────────────

_SELENIUM_READY = False
_driver = None


def _init_selenium():
    """Initialize Selenium Chrome driver with anti-detection + proxy."""
    global _SELENIUM_READY, _driver

    if _SELENIUM_READY:
        return _driver

    import warnings
    warnings.filterwarnings("ignore")

    from seleniumwire import webdriver
    from selenium.webdriver.chrome.service import Service

    CHROME_BINARY = "/tmp/cft/chrome-linux64/chrome"
    DRIVER_PATH = "/tmp/cft/chromedriver-linux64/chromedriver"

    chrome_options = webdriver.ChromeOptions()
    for arg in [
        "--disable-notifications",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    ]:
        chrome_options.add_argument(arg)
    chrome_options.binary_location = CHROME_BINARY
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    seleniumwire_options = None
    if DATAIMPULSE_PROXY:
        seleniumwire_options = {
            "proxy": {
                "http": DATAIMPULSE_PROXY,
                "https": DATAIMPULSE_PROXY,
            },
            "disable_capture": True,  # Suppress request logging
        }
        log.info(f"Using proxy: {DATAIMPULSE_PROXY[:30]}...")
    else:
        seleniumwire_options = {"disable_capture": True}

    _driver = webdriver.Chrome(
        service=Service(DRIVER_PATH),
        options=chrome_options,
        seleniumwire_options=seleniumwire_options,
    )
    _driver.set_page_load_timeout(60)

    # Load cookies
    log.info("Loading Facebook cookies...")
    _driver.get("https://www.facebook.com")
    time.sleep(5)

    cookies_path = Path(FB_COOKIES_PATH)
    if not cookies_path.exists():
        log.error(f"Cookies file not found: {FB_COOKIES_PATH}")
        sys.exit(1)

    cookie_count = 0
    with open(cookies_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                try:
                    cookie = {
                        "name": parts[5],
                        "value": parts[6],
                        "domain": parts[0],
                        "path": parts[2],
                        "secure": parts[3] == "TRUE",
                    }
                    if parts[4] != "0":
                        cookie["expiry"] = int(parts[4])
                    _driver.add_cookie(cookie)
                    cookie_count += 1
                except Exception:
                    pass

    _driver.get("https://www.facebook.com")
    time.sleep(8)

    src_len = len(_driver.page_source)
    log.info(f"Cookies loaded: {cookie_count} | Homepage: {src_len} chars")

    if src_len < 100000:
        log.warning("Homepage source is small — cookies may be invalid/expired!")

    _SELENIUM_READY = True
    return _driver


def _click_sort_recent(driver):
    """Click 'Most recent' tab in comment section to get chronological comments."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
        TimeoutException,
    )

    # Facebook has a sort dropdown or tab for comments
    # Try multiple selectors for "Most recent" / "Terbaru"
    sort_selectors = [
        # English: "Most recent"
        "//span[contains(text(), 'Most recent')]",
        "//a[contains(text(), 'Most recent')]",
        "//div[@role='button' and contains(text(), 'Most recent')]",
        # Indonesian: "Terbaru"
        "//span[contains(text(), 'Terbaru')]",
        "//a[contains(text(), 'Terbaru')]",
        # Sort dropdown trigger
        "//div[@role='button'][.//span[contains(text(), 'Top comments')]]",
        "//div[@role='button'][.//span[contains(text(), 'Komentar teratas')]]",
        "//div[@role='button'][.//span[contains(text(), 'Most relevant')]]",
    ]

    for selector in sort_selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)
            if elements:
                for elem in elements:
                    text = (elem.text or "").strip().lower()
                    if "recent" in text or "terbaru" in text:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                        time.sleep(0.5)
                        elem.click()
                        time.sleep(2)
                        log.info("  Switched to 'Most recent' comment sort")
                        return True
                    elif "top" in text or "teratas" in text or "relevant" in text:
                        # This is the sort dropdown — click to open
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                        time.sleep(0.5)
                        try:
                            elem.click()
                            time.sleep(1)
                            # Now look for "Most recent" option
                            recent_opts = driver.find_elements(By.XPATH, "//span[contains(text(), 'Most recent') or contains(text(), 'Terbaru')]")
                            for opt in recent_opts:
                                opt.click()
                                time.sleep(2)
                                log.info("  Switched to 'Most recent' comment sort")
                                return True
                        except (ElementClickInterceptedException, StaleElementReferenceException):
                            continue
        except (NoSuchElementException, StaleElementReferenceException):
            continue

    log.info("  Could not find sort selector (using default)")
    return False


def _expand_replies(driver, max_expansions=50):
    """Click 'View X replies' buttons to expand sub-comments.
    
    Facebook reply buttons have structure:
      <div role="button" class="x1i10hfl...">
        <span>View all 43 replies</span>
      </div>
    Or:
      <div role="button" class="x1i10hfl...">
        <span>View 1 reply</span>
      </div>
    """
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
    )

    expansions = 0
    max_rounds = 15

    # Facebook reply button keywords (English + Indonesian)
    reply_keywords = [
        "view", "reply", "replies", "balasan", "lihat", "tampilkan",
        "more reply", "more replies", "balasan lain",
    ]

    for round_num in range(max_rounds):
        if expansions >= max_expansions:
            break

        clicked_this_round = False

        # Find all div[role='button'] elements
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, "div[role='button']")
        except Exception:
            break

        for btn in buttons:
            if expansions >= max_expansions:
                break
            try:
                text = (btn.text or "").strip().lower()
                if not text:
                    continue

                # Match reply button patterns: "View all X replies", "View 1 reply", "Lihat X balasan"
                if any(kw in text for kw in reply_keywords):
                    # Also match "View more comments" but DON'T match "View more comments" here
                    if "more comment" in text or "komentar lain" in text:
                        continue

                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.3)
                    try:
                        btn.click()
                        time.sleep(1.5)
                        expansions += 1
                        clicked_this_round = True
                    except (ElementClickInterceptedException, StaleElementReferenceException):
                        # Try JS click as fallback
                        try:
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(1.5)
                            expansions += 1
                            clicked_this_round = True
                        except:
                            continue
            except StaleElementReferenceException:
                continue

        if not clicked_this_round:
            break

        time.sleep(1)

    if expansions > 0:
        log.info(f"  Expanded {expansions} reply threads")
    return expansions


def _load_more_comments(driver, max_comments):
    """Scroll and click 'View more comments' to load comments up to max_comments."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
    )

    scroll_count = 0
    max_scrolls = 40

    while scroll_count < max_scrolls:
        # Check how many comments loaded
        comment_elements = driver.find_elements(
            By.CSS_SELECTOR,
            "div[role='article'][aria-label*='comment' i]"
        )
        if len(comment_elements) >= max_comments:
            log.info(f"  Loaded {len(comment_elements)} comment elements (target: {max_comments})")
            break

        # Try clicking "View more comments" / "Lihat komentar lain"
        clicked = False
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, "div[role='button']")
            for btn in buttons:
                text = (btn.text or "").strip().lower()
                if any(kw in text for kw in [
                    "more comment", "view more comment", "see more comment",
                    "komentar lain", "lihat komentar", "tampilkan komentar",
                    "comment lainnya",
                ]):
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.3)
                    btn.click()
                    time.sleep(2)
                    clicked = True
                    break
        except (ElementClickInterceptedException, StaleElementReferenceException):
            pass

        # Scroll down
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        scroll_count += 1

        if not clicked and scroll_count > 15:
            break

    comment_elements = driver.find_elements(
        By.CSS_SELECTOR,
        "div[role='article'][aria-label*='comment' i]"
    )
    return len(comment_elements)


def _extract_comments_from_dom(driver) -> list[dict]:
    """Extract comments with replies from rendered DOM using fast JS parsing."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
    )

    # Read JS from file to avoid Python string escaping issues
    js_file = Path(__file__).parent / "extract_comments.js"
    if js_file.exists():
        with open(js_file) as f:
            js_script = f.read()
    else:
        js_script = "(function() { return []; })();"

    try:
        raw_result = driver.execute_script(js_script)
        if raw_result and isinstance(raw_result, list) and len(raw_result) > 0:
            log.info(f"  JS extracted {len(raw_result)} comments")
            total_replies = sum(len(c.get("replies", [])) for c in raw_result)
            if total_replies > 0:
                log.info(f"  JS extracted {total_replies} replies")
            
            # Deduplicate
            seen = set()
            unique = []
            for c in raw_result:
                key = (c.get("name", ""), c.get("text", "")[:50])
                if key not in seen:
                    seen.add(key)
                    unique.append(c)
            
            return unique
    except Exception as e:
        log.warning(f"  JS extraction failed: {e}, falling back to Selenium")

    # Fallback: PROVEN Selenium approach from fb-apify-native (221 data points verified)
    log.info(f"  Using proven Selenium extraction (div[role=article] div[role=article])")
    
    import random
    
    comments = []
    replies = []
    
    # Click reply buttons to expand replies (proven selector: span text)
    try:
        reply_btns = driver.find_elements(By.XPATH, 
            "//span[contains(text(), 'Balas') or contains(text(), 'Reply') or "
            "contains(text(), 'Lihat balasan') or contains(text(), 'View')]"
        )
        clicked = 0
        for btn in reply_btns[:20]:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1)
                clicked += 1
            except:
                pass
        if clicked > 0:
            log.info(f"  Clicked {clicked} reply buttons (span text approach)")
    except:
        pass
    
    # Collect top-level comments: div[role='article'] (not nested)
    try:
        all_articles = driver.find_elements(By.CSS_SELECTOR, "div[role='article']")
        nested_articles = driver.find_elements(By.CSS_SELECTOR, "div[role='article'] div[role='article']")
        
        # Top-level = all articles minus nested ones
        nested_set = set(nested_articles)
        top_level = [a for a in all_articles if a not in nested_set]
        
        log.info(f"  Articles: {len(all_articles)} total, {len(top_level)} top-level, {len(nested_articles)} nested")
        
        for elem in top_level:
            try:
                text = elem.text.strip()
                if text and len(text) > 5:
                    lines = text.split('\n')
                    commenter = lines[0].strip() if lines else "Unknown"
                    comment_text = '\n'.join(lines[1:]) if len(lines) > 1 else text
                    comment_text = re.sub(r'\b(suka|like|reply|balas|lihat balasan|view more replies| View more)\b.*$', '', comment_text, flags=re.IGNORECASE).strip()
                    
                    likes = 0
                    likes_match = re.search(r'(\d+)\s*(suka|like)', text.lower())
                    if likes_match:
                        likes = int(likes_match.group(1))
                    
                    ts = ""
                    ts_match = re.search(r'(\d+\s*(jam|menit|detik|hari|minggu|bulan|tahun|h|m|d|w|mo|y|s)\b|(?:Just now|Baru saja|\d+h|\d+m|\d+d))', text)
                    if ts_match:
                        ts = ts_match.group(0)
                    
                    if comment_text and len(comment_text) > 3:
                        comments.append({
                            "name": commenter,
                            "text": comment_text[:500],
                            "likes_count": likes,
                            "timestamp": ts,
                            "comment_id": "",
                            "replies": []
                        })
            except:
                pass
    except:
        pass
    
    # Collect replies: div[role='article'] div[role='article'] (nested!)
    try:
        reply_elements = driver.find_elements(By.CSS_SELECTOR, "div[role='article'] div[role='article']")
        log.info(f"  Found {len(reply_elements)} nested reply elements")
        
        for elem in reply_elements:
            try:
                text = elem.text.strip()
                if text and len(text) > 5:
                    lines = text.split('\n')
                    replier = lines[0].strip() if lines else "Unknown"
                    reply_text = '\n'.join(lines[1:]) if len(lines) > 1 else text
                    reply_text = re.sub(r'\b(suka|like|reply|balas)\b.*$', '', reply_text, flags=re.IGNORECASE).strip()
                    
                    if reply_text and len(reply_text) > 3:
                        # Try to match to nearest preceding top-level comment
                        # For now, attach to last comment (simplified from proven approach)
                        if comments:
                            comments[-1]["replies"].append({
                                "name": replier,
                                "text": reply_text[:500],
                            })
            except:
                pass
    except:
        pass
    
    # Deduplicate
    seen = set()
    unique = []
    for c in comments:
        key = (c.get("name", ""), c.get("text", "")[:50])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    
    return unique
