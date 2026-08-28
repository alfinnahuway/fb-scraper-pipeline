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
    _driver.set_page_load_timeout(30)

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
    """Click 'View X replies' buttons to expand sub-comments."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
    )

    expansions = 0
    max_rounds = 10  # Maximum rounds of expansion

    for round_num in range(max_rounds):
        if expansions >= max_expansions:
            break

        clicked_this_round = False

        # Facebook "View X replies" / "Lihat X balasan" selectors
        reply_selectors = [
            "div[role='button']",
            "a[role='button']",
        ]

        reply_keywords = [
            "view", "reply", "replies", "balasan", "lihat", "tampilkan",
            "more reply", "more replies", "balasan lain",
        ]

        for selector in reply_selectors:
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in buttons:
                    if expansions >= max_expansions:
                        break
                    try:
                        text = (btn.text or "").strip().lower()
                        if any(kw in text for kw in reply_keywords):
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            time.sleep(0.3)
                            btn.click()
                            time.sleep(1.5)
                            expansions += 1
                            clicked_this_round = True
                    except (ElementClickInterceptedException, StaleElementReferenceException):
                        continue
            except Exception:
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
    """Extract comments with replies from rendered DOM."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
    )

    comments = []

    # Get all top-level comment containers
    comment_elements = driver.find_elements(
        By.CSS_SELECTOR,
        "div[role='article'][aria-label*='comment' i]"
    )

    log.info(f"  Processing {len(comment_elements)} comment elements...")

    for elem in comment_elements:
        try:
            comment = _parse_comment_element(elem)
            if comment and comment.get("text"):
                # Look for replies within this comment's parent container
                comment["replies"] = _extract_replies(elem)
                comments.append(comment)
        except StaleElementReferenceException:
            continue

    # Deduplicate
    seen = set()
    unique = []
    for c in comments:
        key = (c.get("name", ""), c.get("text", "")[:50])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


def _extract_replies(parent_elem) -> list[dict]:
    """Extract reply comments from a parent comment's container."""
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
    )

    replies = []

    # Replies are typically nested within the comment's parent container
    # Try multiple strategies to find reply elements
    try:
        # Strategy 1: Look for nested article elements with "reply" aria-label
        reply_elements = parent_elem.find_elements(
            By.CSS_SELECTOR,
            "div[role='article'][aria-label*='reply' i]"
        )

        # Strategy 2: If no reply labels, look for nested articles
        if not reply_elements:
            reply_elements = parent_elem.find_elements(
                By.CSS_SELECTOR,
                "div[role='article']"
            )
            # Filter: only keep elements that are NOT the parent comment itself
            reply_elements = [r for r in reply_elements if r != parent_elem]

        for reply_elem in reply_elements:
            try:
                reply = _parse_comment_element(reply_elem)
                if reply and reply.get("text"):
                    replies.append(reply)
            except StaleElementReferenceException:
                continue

    except StaleElementReferenceException:
        pass

    return replies


def _parse_comment_element(elem) -> dict:
    """Parse a single comment or reply element from DOM."""
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
    }

    try:
        # Author name
        try:
            name_el = elem.find_element(By.CSS_SELECTOR, "a[role='link'] span, a span, h3 span")
            result["name"] = name_el.text.strip()
        except NoSuchElementException:
            pass

        # Comment text — try multiple selectors
        if not result["text"]:
            for text_selector in [
                "div[dir='auto'] span",
                "div[dir='auto']",
                "span[dir='auto']",
                "div[data-ad-comet-preview='message'] span",
                "div[style*='text-align'] span",
            ]:
                try:
                    text_elements = elem.find_elements(By.CSS_SELECTOR, text_selector)
                    texts = []
                    for te in text_elements:
                        t = te.text.strip()
                        if t and t != result["name"] and len(t) > 2:
                            texts.append(t)
                    if texts:
                        result["text"] = " ".join(texts)
                        break
                except NoSuchElementException:
                    continue

        # Likes count
        try:
            for like_selector in [
                "[aria-label*='reaction' i] span",
                "span[class*='reaction']",
                "div[role='button'][aria-label*='like' i]",
                "span[data-content]",
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

        # Timestamp
        try:
            for time_selector in ["abbr", "time", "span[class*='timestamp']"]:
                time_el = elem.find_element(By.CSS_SELECTOR, time_selector)
                result["timestamp"] = time_el.get_attribute("title") or time_el.get_attribute("datetime") or time_el.text.strip()
                if result["timestamp"]:
                    break
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


def scrape_comments(post_url: str, max_comments: int = 150,
                    sort_mode: str = "top", expand_replies: bool = True) -> list[dict]:
    """
    Scrape comments from a Facebook post using native Selenium.

    Args:
        post_url: Facebook post URL (numeric ID format preferred)
        max_comments: Maximum top-level comments to scrape
        sort_mode: 'top' (by engagement) or 'recent' (chronological)
        expand_replies: Whether to expand reply threads

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

    # Switch to "Most recent" sort if requested
    if sort_mode == "recent":
        _click_sort_recent(driver)
        time.sleep(3)

    # Load more comments up to max_comments
    loaded = _load_more_comments(driver, max_comments)
    log.info(f"  Loaded {loaded} comment elements")

    # Expand reply threads
    if expand_replies:
        _expand_replies(driver, max_expansions=50)

    # Extract comments + replies from DOM
    comments = _extract_comments_from_dom(driver)

    # Count replies
    total_replies = sum(len(c.get("replies", [])) for c in comments)
    log.info(f"  Extracted {len(comments)} comments + {total_replies} replies")

    return comments


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
                 output_path: str = None, skip_comments: bool = False,
                 comment_sort: str = "top", expand_replies: bool = True) -> dict:
    """Run the full pipeline: search → extract post_id → scrape comments → output JSON."""
    start_time = time.time()
    log.info(f"{'='*60}")
    log.info(f"Pipeline started")
    log.info(f"  Keyword:        {keyword}")
    log.info(f"  Max posts:      {max_posts}")
    log.info(f"  Max comments:   {max_comments}")
    log.info(f"  Comment sort:   {comment_sort}")
    log.info(f"  Expand replies: {expand_replies}")
    log.info(f"{'='*60}")

    # Step 1: Search posts
    log.info(f"\n[Step 1] Searching posts via Apify danek actor...")
    posts = search_posts(keyword, max_posts, since, until)
    log.info(f"  Found {len(posts)} posts")

    if not posts:
        log.warning("No posts found. Exiting.")
        return {"error": "No posts found", "keyword": keyword}

    for i, p in enumerate(posts):
        cid = p.get("comments_count", 0)
        log.info(f"  Post {i+1}: {p['author']} | post_id={p['post_id']} | comments={cid}")
        log.info(f"    URL: {p['numeric_url'][:80] or p['pfbid_url'][:80]}")

    # Step 2: Scrape comments
    if not skip_comments:
        log.info(f"\n[Step 2] Scraping comments via native Selenium (sort={comment_sort})...")
        total_comments = 0
        total_replies = 0

        for i, post in enumerate(posts):
            url = post.get("numeric_url") or post.get("pfbid_url", "")
            if not url:
                log.warning(f"  Post {i+1}: No URL available, skipping")
                continue

            if not post.get("post_id"):
                log.warning(f"  Post {i+1}: No numeric post_id, URL may not work (pfbid format)")

            if post.get("comments_count", 0) == 0:
                log.info(f"  Post {i+1}: 0 comments reported, skipping")
                post["comments"] = []
                continue

            comments = scrape_comments(
                url,
                max_comments=max_comments,
                sort_mode=comment_sort,
                expand_replies=expand_replies,
            )
            post["comments"] = comments
            post_comments = len(comments)
            post_replies = sum(len(c.get("replies", [])) for c in comments)
            total_comments += post_comments
            total_replies += post_replies
            log.info(f"  Post {i+1}: {post_comments} comments + {post_replies} replies (total: {total_comments}c+{total_replies}r)")

            if i < len(posts) - 1:
                wait_time = 3
                log.info(f"  Waiting {wait_time}s...")
                time.sleep(wait_time)

        _close_selenium()
        log.info(f"\nTotal: {total_comments} comments + {total_replies} replies")
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
        "total_replies": sum(
            sum(len(c.get("replies", [])) for c in p.get("comments", []))
            for p in posts
        ),
        "comment_sort": comment_sort if not skip_comments else None,
        "posts": posts,
    }

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
    log.info(f"  Total replies:  {result['total_replies']}")
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
  python fb_pipeline.py --keyword "DPR RI" --max-posts 5 --max-comments 100
  python fb_pipeline.py --keyword "Pilpres" --comment-sort recent --max-comments 200
  python fb_pipeline.py --keyword "INFRASTRUKTUR" --skip-comments
        """,
    )
    parser.add_argument("--keyword", required=True, help="Search keyword")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help=f"Max posts to retrieve (default: {DEFAULT_MAX_POSTS})")
    parser.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS, help=f"Max comments per post (default: {DEFAULT_MAX_COMMENTS})")
    parser.add_argument("--since", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=None, help="Output file path")
    parser.add_argument("--skip-comments", action="store_true", help="Skip comment scraping (posts only)")
    parser.add_argument("--comment-sort", choices=["top", "recent"], default="top",
                        help="Comment sort: 'top' (by engagement) or 'recent' (chronological). Default: top")
    parser.add_argument("--no-replies", action="store_true", help="Skip expanding reply threads")
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
        comment_sort=args.comment_sort,
        expand_replies=not args.no_replies,
    )

    print(f"\nDone! Output: {args.output or 'auto-generated'}")
    print(f"Posts: {result.get('total_posts', 0)} | Comments: {result.get('total_comments', 0)} | Replies: {result.get('total_replies', 0)}")


if __name__ == "__main__":
    main()
