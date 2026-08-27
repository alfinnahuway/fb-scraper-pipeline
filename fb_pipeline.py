"""
Facebook Scraper Pipeline — Main Script
========================================
Hybrid approach:
  1. Apify danek actor for keyword search (~$0.05/run)
  2. Native Selenium + cookies for comment scraping ($0)

Usage:
  python fb_pipeline.py --keyword "DPR RI" --max-posts 20 --max-comments 150
  python fb_pipeline.py --keyword "Pilpres" --max-posts 50 --max-comments 300 --output ./output/custom.json
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

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


# ─── Step 1: Search posts via Apify danek actor ──────────────────────────────

def search_posts(keyword: str, max_posts: int, since: str = None, until: str = None) -> list[dict]:
    """
    Search Facebook posts using danek/facebook-search-ppr actor.
    Returns list of post dicts with normalized fields including numeric post_id.
    """
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
        # Extract page name from author object or URL
        author_obj = item.get("author") or {}
        author_name = author_obj.get("name", "") if isinstance(author_obj, dict) else str(author_obj)
        
        # Extract page username from URL for building numeric URL
        url = item.get("url", "")
        page_slug = _extract_page_slug(url)
        
        post = {
            "post_id": str(item.get("post_id", "")),
            "pfbid_url": url,
            "numeric_url": "",  # Will be built below
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
        
        # Build numeric URL (this is the key step — pfbid doesn't work, but numeric ID does)
        if post["post_id"] and page_slug:
            post["numeric_url"] = f"https://www.facebook.com/{page_slug}/posts/{post['post_id']}"
        
        posts.append(post)

    return posts


def _extract_page_slug(url: str) -> str:
    """Extract page slug from Facebook URL (e.g. KompasTV from /KompasTV/posts/...)."""
    match = re.search(r"facebook\.com/([^/]+)/posts/", url)
    if match:
        return match.group(1)
    # Try reel/video format
    match = re.search(r"facebook\.com/([^/]+)/(?:videos|reel)/", url)
    if match:
        return match.group(1)
    return ""


# ─── Step 2: Scrape comments via Native Selenium ─────────────────────────────

# Selenium imports (deferred until needed to keep startup fast)
_SELENIUM_READY = False
_driver = None


def _init_selenium():
    """Initialize Selenium Chrome driver with anti-detection + proxy."""
    global _SELENIUM_READY, _driver
    
    if _SELENIUM_READY:
        return _driver

    import warnings
    warnings.filterwarnings("ignore")
    
    import time
    from seleniumwire import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

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

    # Proxy (optional)
    seleniumwire_options = {}
    if DATAIMPULSE_PROXY:
        seleniumwire_options = {
            "proxy": {
                "http": DATAIMPULSE_PROXY,
                "https": DATAIMPULSE_PROXY,
            }
        }
        log.info(f"Using proxy: {DATAIMPULSE_PROXY[:30]}...")

    _driver = webdriver.Chrome(
        service=Service(DRIVER_PATH),
        options=chrome_options,
        seleniumwire_options=seleniumwire_options if seleniumwire_options else None,
    )

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


def scrape_comments(post_url: str, max_comments: int = 150) -> list[dict]:
    """
    Scrape comments from a Facebook post using native Selenium.
    
    Args:
        post_url: Facebook post URL (numeric ID format preferred)
        max_comments: Maximum comments to scrape
        
    Returns:
        List of comment dicts with: name, text, likes, timestamp, replies
    """
    driver = _init_selenium()
    
    log.info(f"  Scraping: {post_url[:70]}...")
    driver.get(post_url)
    time.sleep(8)

    src = driver.page_source
    src_len = len(src)
    
    if src_len < 100000:
        log.warning(f"  Page source too small ({src_len} chars) — post may not be accessible")
        return []

    # Find and click "View more comments" / "See more comments" button repeatedly
    comments = _scroll_for_comments(driver, max_comments)
    
    # Extract comments from DOM
    comments = _extract_comments_from_dom(driver)
    
    log.info(f"  Extracted {len(comments)} comments")
    return comments


def _scroll_for_comments(driver, max_comments: int):
    """Scroll page and click 'load more comments' buttons to load all comments."""
    import time
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
    )

    scroll_count = 0
    max_scrolls = 50  # Safety limit

    while scroll_count < max_scrolls:
        # Try clicking "View more comments" buttons
        clicked = False
        try:
            # Facebook uses various comment loading button selectors
            selectors = [
                "div[role='button']",
                "a[href*='comment_id']",
                "div[data-sigil='comment-load-more']",
                "[aria-label*='comment' i]",
                "div[role='button'][tabindex]:not([aria-hidden='true'])",
            ]

            for selector in selectors:
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        text = (btn.text or "").lower()
                        if any(kw in text for kw in ["more comment", "see more", "view more", "load more", "comment lain", "lainnya"]):
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            time.sleep(0.5)
                            btn.click()
                            time.sleep(2)
                            clicked = True
                            break
                    if clicked:
                        break
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
        except (ElementClickInterceptedException, StaleElementReferenceException):
            pass

        # Scroll down to trigger lazy loading
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        scroll_count += 1

        # Check if we have enough comments loaded
        comment_elements = driver.find_elements(By.CSS_SELECTOR, "[data-commentid], div[role='article']")
        if len(comment_elements) >= max_comments:
            break

        if not clicked and scroll_count > 10:
            # No more buttons to click and scrolled enough
            break

    return scroll_count


def _extract_comments_from_dom(driver) -> list[dict]:
    """Extract comments from the rendered DOM."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
    )

    comments = []

    # Facebook comment containers — try multiple selectors
    comment_selectors = [
        "div[role='article'][aria-label*='comment' i]",
        "div[role='article'] ul li",
        "div[data-commentid]",
        "div[role='article']",
    ]

    comment_elements = []
    for selector in comment_selectors:
        comment_elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if comment_elements:
            log.info(f"  Using selector: {selector} ({len(comment_elements)} elements)")
            break

    for elem in comment_elements:
        try:
            comment = _parse_comment_element(elem)
            if comment and comment.get("text"):
                comments.append(comment)
        except StaleElementReferenceException:
            continue

    # Deduplicate by comment text
    seen = set()
    unique_comments = []
    for c in comments:
        key = (c.get("name", ""), c.get("text", ""))
        if key not in seen:
            seen.add(key)
            unique_comments.append(c)

    return unique_comments


def _parse_comment_element(elem) -> dict:
    """Parse a single comment element from DOM."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
    )

    result = {
        "name": "",
        "text": "",
        "likes_count": 0,
        "timestamp": "",
        "comment_id": "",
        "replies": [],
    }

    try:
        # Comment author name — usually in <a> tag with role="link"
        try:
            name_el = elem.find_element(By.CSS_SELECTOR, "a[role='link'] span, a span")
            result["name"] = name_el.text.strip()
        except NoSuchElementException:
            pass

        # Comment text — usually in a span or div after author name
        try:
            # Try multiple text selectors
            for text_selector in [
                "div[dir='auto'] span",
                "div[dir='auto']",
                "span[dir='auto']",
                "div[data-ad-comet-preview='message'] span",
            ]:
                text_elements = elem.find_elements(By.CSS_SELECTOR, text_selector)
                for te in text_elements:
                    text = te.text.strip()
                    if text and len(text) > 3 and text != result["name"]:
                        result["text"] = text
                        break
                if result["text"]:
                    break
        except NoSuchElementException:
            pass

        # Likes count — look for reaction/like count
        try:
            for like_selector in [
                "span[data-content='likes_count']",
                "[aria-label*='reaction' i] span",
                "span[class*='reaction']",
                "div[role='button'][aria-label*='like' i]",
            ]:
                like_elements = elem.find_elements(By.CSS_SELECTOR, like_selector)
                for le in like_elements:
                    text = le.text.strip()
                    nums = re.findall(r"[\d,.]+", text)
                    if nums:
                        result["likes_count"] = int(nums[0].replace(",", "").replace(".", ""))
                        break
                if result["likes_count"]:
                    break
        except NoSuchElementException:
            pass

        # Timestamp — look for time-related elements
        try:
            time_el = elem.find_element(By.CSS_SELECTOR, "abbr, time, span[class*='timestamp']")
            result["timestamp"] = time_el.get_attribute("title") or time_el.text.strip()
        except NoSuchElementException:
            pass

        # Comment ID
        try:
            cid = elem.get_attribute("data-commentid") or elem.get_attribute("id")
            if cid:
                result["comment_id"] = cid
        except Exception:
            pass

    except StaleElementReferenceException:
        pass

    return result


def _close_selenium():
    """Clean up Selenium driver."""
    global _driver, _SELENIUM_READY
    if _driver:
        _driver.quit()
        _driver = None
        _SELENIUM_READY = False


# ─── Step 3: Run full pipeline ────────────────────────────────────────────────

def run_pipeline(keyword: str, max_posts: int, max_comments: int, 
                 since: str = None, until: str = None,
                 output_path: str = None, skip_comments: bool = False) -> dict:
    """
    Run the full pipeline:
    1. Search posts via Apify danek actor
    2. Scrape comments via native Selenium
    3. Output JSON
    """
    start_time = time.time()
    log.info(f"{'='*60}")
    log.info(f"Pipeline started")
    log.info(f"  Keyword:     {keyword}")
    log.info(f"  Max posts:   {max_posts}")
    log.info(f"  Max comments: {max_comments}")
    log.info(f"{'='*60}")

    # Step 1: Search posts
    log.info(f"\n[Step 1] Searching posts via Apify danek actor...")
    posts = search_posts(keyword, max_posts, since, until)
    log.info(f"  Found {len(posts)} posts")

    if not posts:
        log.warning("No posts found. Exiting.")
        return {"error": "No posts found", "keyword": keyword}

    # Log post summary
    for i, p in enumerate(posts):
        cid = p.get("comments_count", 0)
        log.info(f"  Post {i+1}: {p['author']} | post_id={p['post_id']} | comments={cid}")
        log.info(f"    URL: {p['numeric_url'][:80] or p['pfbid_url'][:80]}")

    # Step 2: Scrape comments
    if not skip_comments:
        log.info(f"\n[Step 2] Scraping comments via native Selenium...")
        total_comments = 0
        
        for i, post in enumerate(posts):
            # Use numeric URL (works with Selenium), fallback to pfbid
            url = post.get("numeric_url") or post.get("pfbid_url", "")
            if not url:
                log.warning(f"  Post {i+1}: No URL available, skipping")
                continue

            if not post.get("post_id"):
                log.warning(f"  Post {i+1}: No numeric post_id, URL may not work (pfbid format)")
            
            # Skip posts with 0 comments
            if post.get("comments_count", 0) == 0:
                log.info(f"  Post {i+1}: 0 comments reported, skipping")
                post["comments"] = []
                continue

            comments = scrape_comments(url, max_comments)
            post["comments"] = comments
            total_comments += len(comments)
            log.info(f"  Post {i+1}: {len(comments)} comments scraped (total: {total_comments})")

            # Rate limit: wait between posts
            if i < len(posts) - 1:
                wait_time = 3
                log.info(f"  Waiting {wait_time}s before next post...")
                time.sleep(wait_time)

        _close_selenium()
        log.info(f"\nTotal comments scraped: {total_comments}")
    else:
        log.info(f"\n[Step 2] Skipping comment scraping (--skip-comments)")
        for post in posts:
            post["comments"] = []

    # Step 3: Build output
    elapsed = time.time() - start_time
    result = {
        "keyword": keyword,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total_posts": len(posts),
        "total_comments": sum(len(p.get("comments", [])) for p in posts),
        "posts": posts,
    }

    # Save output
    if not output_path:
        safe_keyword = re.sub(r"[^\w]+", "_", keyword)
        output_path = str(OUTPUT_DIR / f"{safe_keyword}_{int(time.time())}.json")

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info(f"\n{'='*60}")
    log.info(f"Pipeline complete!")
    log.info(f"  Total posts:    {result['total_posts']}")
    log.info(f"  Total comments: {result['total_comments']}")
    log.info(f"  Elapsed:        {elapsed:.1f}s")
    log.info(f"  Output:         {output_file}")
    log.info(f"{'='*60}")

    return result


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Facebook Scraper Pipeline — keyword search + comment extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fb_pipeline.py --keyword "DPR RI" --max-posts 20 --max-comments 150
  python fb_pipeline.py --keyword "Pilpres 2029" --max-posts 50 --output ./output/pilpres.json
  python fb_pipeline.py --keyword "INFRASTRUKTUR" --skip-comments  # Posts only
        """,
    )
    parser.add_argument("--keyword", required=True, help="Search keyword")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help=f"Max posts to retrieve (default: {DEFAULT_MAX_POSTS})")
    parser.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS, help=f"Max comments per post (default: {DEFAULT_MAX_COMMENTS})")
    parser.add_argument("--since", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=None, help="Output file path (default: ./output/<keyword>_<timestamp>.json)")
    parser.add_argument("--skip-comments", action="store_true", help="Skip comment scraping (posts only)")
    parser.add_argument("--actor", default=SEARCH_ACTOR, help=f"Apify search actor (default: {SEARCH_ACTOR})")

    args = parser.parse_args()

    result = run_pipeline(
        keyword=args.keyword,
        max_posts=args.max_posts,
        max_comments=args.max_comments,
        since=args.since,
        until=args.until,
        output_path=args.output,
        skip_comments=args.skip_comments,
    )

    print(f"\nDone! Output: {args.output or 'auto-generated'}")
    print(f"Posts: {result.get('total_posts', 0)} | Comments: {result.get('total_comments', 0)}")


if __name__ == "__main__":
    main()
