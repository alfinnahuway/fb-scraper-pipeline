# Facebook Scraper Pipeline

Reliable Facebook scraping pipeline: **keyword search → post metadata → comments extraction**.

## How It Works

```
1. danek/facebook-search-ppr  (Apify actor)  → Search keyword, get posts + numeric post IDs
2. apify/facebook-comments-scraper          → Scrape comments per post using numeric ID
3. Output JSON                               → Consolidated results with posts + comments
```

### Key Discovery

Facebook pfbid URLs (`/posts/pfbidXXX`) don't render comment sections in browsers or scrapers. However, the `danek` actor returns a **`post_id` field** containing the **numeric post ID**. By reconstructing the URL as `/{page}/posts/{numeric_id}`, comments become fully scrapable.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your Apify API token + cookies path

# 3. Export Facebook cookies
# Use "Get cookies.txt LOCALLY" browser extension while logged into Facebook
# Save as fb_cookies.txt in project root

# 4. Run pipeline
python fb_pipeline.py --keyword "DPR RI" --max-posts 20 --max-comments 150
```

## Usage

```bash
# Basic search
python fb_pipeline.py --keyword "DPR RI" --max-posts 20 --max-comments 150

# With date range
python fb_pipeline.py --keyword "Pilpres 2029" --max-posts 50 --max-comments 300 --since 2026-01-01 --until 2026-12-31

# Custom output path
python fb_pipeline.py --keyword "INFRASTRUKTUR" --output ./output/custom.json
```

### Arguments

| Argument | Default | Description |
|:---------|:--------|:------------|
| `--keyword` | Required | Search keyword |
| `--max-posts` | 20 | Max posts to retrieve from search |
| `--max-comments` | 150 | Max comments per post |
| `--since` | None | Start date (YYYY-MM-DD) |
| `--until` | None | End date (YYYY-MM-DD) |
| `--output` | ./output/results.json | Output file path |
| `--actor` | danek/facebook-search-ppr | Apify search actor to use |
| `--skip-comments` | False | Skip comment scraping (posts only) |

## Output Format

```json
{
  "keyword": "DPR RI",
  "scraped_at": "2026-08-27T19:00:00",
  "total_posts": 5,
  "total_comments": 750,
  "posts": [
    {
      "post_id": "1756707592485842",
      "url": "https://www.facebook.com/KompasTV/posts/1756707592485842",
      "pfbid_url": "https://www.facebook.com/KompasTV/posts/pfbid0J3CHs4yQDGh...",
      "author": "Kompas TV",
      "author_id": "100044398540615",
      "message": "Ketua DPR RI Puan Maharani mengaku...",
      "timestamp": 1787734899,
      "comments_count": 2553,
      "reactions_count": 4954,
      "reshare_count": 320,
      "comments": [
        {
          "name": "User Name",
          "text": "Comment text...",
          "likes_count": 245,
          "comment_id": "123456789",
          "timestamp": "2026-08-27T15:00:00",
          "replies": [...]
        }
      ]
    }
  ]
}
```

## Requirements

- **Apify account** with API token (~$0.01-0.50 per run depending on volume)
- **Facebook cookies** from a logged-in desktop browser session
- **Python 3.10+**

## Cost Estimation

| Operation | Apify Actor | Cost |
|:----------|:------------|:-----|
| Search (20 posts) | danek/facebook-search-ppr | ~$0.05 |
| Comments (150 per post × 20 posts) | apify/facebook-comments-scraper | ~$0.30 |
| **Total per run** | | **~$0.35** |

## Project Structure

```
fb-scraper-pipeline/
├── fb_pipeline.py        # Main pipeline script
├── config.py             # Configuration loader
├── .env.example          # Environment template
├── fb_cookies.txt        # Facebook cookies (you provide)
├── requirements.txt      # Python dependencies
├── README.md             # This file
└── output/               # Scraping results
    └── results.json
```

## Notes

- pfbid URLs cannot be decoded to numeric IDs via browser scraping. The `danek` actor resolves this by returning `post_id` directly from Facebook's internal API.
- Reel/video URLs (`/reel/`, `/videos/`) work with both yt-dlp and Apify comments scraper.
- Cookie validity: ~1-2 years (c_user, xs) | 90 days (fr) | 2 years (datr). Re-export when expired.
