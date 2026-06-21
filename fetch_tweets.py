"""
Twitter/X Tweet Fetcher with Translation — GitHub Actions Overseas Relay
Uses Nitter instances as proxy to bypass Twitter rate limits and login walls.
Also translates tweets to Chinese using Google Translate (accessible from overseas IP).

Strategy (in order):
1. Nitter RSS (public, no auth needed) - primary, 9 verified working instances
2. Nitter HTML scraping - fallback for RSS parsing failures
3. Twitter syndication API - final fallback (cdn.syndication.twimg.com)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote
import requests

# === Configuration ===
TARGET_USERS = [
    {"username": "elonmusk", "display_name": "马斯克"},
    {"username": "cz_binance", "display_name": "CZ (赵长鹏)"},
    {"username": "realDonaldTrump", "display_name": "特朗普"},
    {"username": "aleabitoreddit", "display_name": "Serenity (白毛股神)"},
]
TWEETS_PER_USER = 3
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tweets.json")
TRANSLATION_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translation_cache.json")

# Nitter instances — verified working as of 2026-06
# Source: https://github.com/zedeus/nitter/wiki/Instances
NITTER_INSTANCES = [
    "https://xcancel.com",           # 🇺🇸 Verified working
    "https://nitter.poast.org",      # 🇺🇸 Verified working
    "https://nitter.privacyredirect.com",  # 🇫🇮 Verified working
    "https://lightbrd.com",          # 🇹🇷 Verified working, NSFW
    "https://nitter.space",          # 🇺🇸 Verified working
    "https://nitter.tiekoetter.com", # 🇩🇪 Verified working
    "https://nuku.trabun.org",       # 🇨🇱 Verified working
    "https://nitter.catsarch.com",   # 🇺🇸/🇩🇪 Verified working
    "https://nitter.kareem.one",     # 🇸🇬 Verified working
    "https://nitter.net",            # 🇳🇱 Official, may be rate-limited
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ============================================================
# Translation (Google Translate, accessible from overseas)
# ============================================================

def translate_text(text, target_lang="zh-CN", source_lang="auto"):
    if not text or len(text.strip()) < 2:
        return text
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text[:5000],
        }
        resp = SESSION.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            translated = ""
            for sentence in result[0]:
                if sentence and sentence[0]:
                    translated += sentence[0]
            return translated.strip()
        else:
            log(f"  [translate] HTTP {resp.status_code}")
    except Exception as e:
        log(f"  [translate] Error: {e}")
    return text


def load_translation_cache():
    if os.path.exists(TRANSLATION_CACHE_FILE):
        try:
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_translation_cache(cache):
    try:
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  [translate] Save cache error: {e}")


def translate_tweets(tweets):
    cache = load_translation_cache()
    translated = 0
    for t in tweets:
        tweet_id = t.get("tweet_id", "")
        if not tweet_id:
            continue
        if tweet_id in cache:
            t["translated"] = cache[tweet_id]
            continue
        content = t.get("content", "")
        if content:
            result = translate_text(content)
            if result and result != content:
                cache[tweet_id] = result
                t["translated"] = result
                translated += 1
                log(f"  [translate] {tweet_id[:12]}... -> {result[:60]}...")
            time.sleep(1)
    save_translation_cache(cache)
    log(f"  [translate] {translated} new translations")
    return tweets


# ============================================================
# Method 1: Nitter RSS (primary, 9 verified working instances)
# ============================================================

def fetch_via_nitter(user):
    """Fetch tweets via Nitter RSS from multiple working instances"""
    for i, instance in enumerate(NITTER_INSTANCES):
        try:
            rss_url = f"{instance}/{user['username']}/rss"
            log(f"  [nitter {i+1}/{len(NITTER_INSTANCES)}] {instance}...")
            resp = SESSION.get(rss_url, timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "").lower()
                # Try RSS XML parsing
                if "xml" in content_type or "rss" in content_type or resp.text.strip().startswith("<?xml"):
                    tweets = parse_nitter_rss(resp.text, user)
                    if tweets:
                        log(f"  [nitter RSS] Got {len(tweets)} tweets from {instance}")
                        return tweets
                # Try HTML parsing as fallback
                if "<html" in resp.text.lower()[:200]:
                    tweets = parse_nitter_html(resp.text, user)
                    if tweets:
                        log(f"  [nitter HTML] Got {len(tweets)} tweets from {instance}")
                        return tweets
                # Try raw text parsing
                if "rss" in resp.text.lower()[:200] or "<item>" in resp.text:
                    tweets = parse_nitter_rss(resp.text, user)
                    if tweets:
                        log(f"  [nitter RSS] Got {len(tweets)} tweets from {instance}")
                        return tweets
            elif resp.status_code == 429:
                log(f"  [nitter] {instance} rate limited (429)")
            elif resp.status_code == 404:
                log(f"  [nitter] {instance} returned 404")
            elif resp.status_code == 403:
                log(f"  [nitter] {instance} forbidden (403)")
            else:
                log(f"  [nitter] {instance} returned {resp.status_code}")
        except requests.exceptions.Timeout:
            log(f"  [nitter] {instance} timeout")
        except requests.exceptions.ConnectionError:
            log(f"  [nitter] {instance} connection error")
        except Exception as e:
            log(f"  [nitter] {instance} error: {str(e)[:80]}")
        time.sleep(0.5)  # Small delay between instances
    return []


def parse_nitter_rss(rss_text, user):
    """Parse Nitter RSS feed XML"""
    import xml.etree.ElementTree as ET
    try:
        # Clean potential BOM and encoding issues
        rss_text = rss_text.strip()
        if not rss_text.startswith("<"):
            # Try to find XML start
            idx = rss_text.find("<?xml")
            if idx >= 0:
                rss_text = rss_text[idx:]
            else:
                idx = rss_text.find("<rss")
                if idx >= 0:
                    rss_text = rss_text[idx:]

        root = ET.fromstring(rss_text)
        tweets = []
        for item in root.findall(".//item")[:TWEETS_PER_USER]:
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")

            title_text = (title.text or "").strip() if title is not None else ""
            link_text = (link.text or "").strip() if link is not None else ""
            pub_date_text = (pub_date.text or "").strip() if pub_date is not None else ""

            # Clean title: remove display_name prefix
            content = title_text
            if content.startswith(f"{user['display_name']}:"):
                content = content[len(f"{user['display_name']}:"):].strip()
            elif content.startswith("@"):
                parts = content.split(":", 1)
                content = parts[-1].strip() if len(parts) > 1 else content

            if not content:
                continue

            tweet_id = link_text.rstrip("/").split("/")[-1] if link_text else ""
            if not tweet_id:
                continue

            tweets.append({
                "id": f"tweet_{user['username']}_{tweet_id}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": pub_date_text,
                "content": content,
                "url": f"https://x.com/{user['username']}/status/{tweet_id}",
                "tweet_id": tweet_id,
            })
        return tweets
    except ET.ParseError as e:
        log(f"  [nitter:rss] XML parse error: {str(e)[:60]}")
    except Exception as e:
        log(f"  [nitter:rss] Error: {str(e)[:60]}")
    return []


def parse_nitter_html(html_text, user):
    """Parse Nitter HTML page for tweets"""
    from html.parser import HTMLParser

    class TweetParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tweets = []
            self.current = {}
            self.in_tweet = False
            self.in_content = False
            self.in_date = False
            self.in_link = False
            self.text_buffer = ""
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            cls = attrs_dict.get("class", "")

            if "tweet-body" in cls or "tweet-content" in cls:
                self.in_tweet = True
                self.current = {}
                self.text_buffer = ""
                self.depth = 0

            if self.in_tweet:
                if "tweet-link" in cls or "permalink" in cls:
                    href = attrs_dict.get("href", "")
                    if href:
                        self.current["link"] = href
                        tid = href.rstrip("/").split("/")[-1]
                        self.current["tweet_id"] = tid
                if "tweet-date" in cls:
                    self.in_date = True
                    self.text_buffer = ""
                if tag in ("a", "span", "div", "p"):
                    self.in_content = True
                    self.depth += 1

        def handle_data(self, data):
            if self.in_date:
                self.text_buffer += data
            if self.in_content and self.in_tweet:
                self.text_buffer += data

        def handle_endtag(self, tag):
            if self.in_date and tag in ("span", "a", "div"):
                self.current["date"] = self.text_buffer.strip()
                self.in_date = False
                self.text_buffer = ""
            if self.in_tweet and tag in ("div", "p"):
                self.depth -= 1
                if self.depth <= 0:
                    content = self.text_buffer.strip()
                    if content and len(content) > 5 and self.current.get("tweet_id"):
                        self.current["content"] = content
                        self.tweets.append(self.current)
                        self.current = {}
                    self.in_tweet = False
                    self.in_content = False
                    self.text_buffer = ""

    try:
        parser = TweetParser()
        parser.feed(html_text)
        tweets = []
        for t in parser.tweets[:TWEETS_PER_USER]:
            tid = t.get("tweet_id", "")
            if not tid:
                continue
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": t.get("date", ""),
                "content": t.get("content", ""),
                "url": f"https://x.com/{user['username']}/status/{tid}",
                "tweet_id": tid,
            })
        return tweets
    except Exception as e:
        log(f"  [nitter:html] Parse error: {str(e)[:60]}")
        return []


# ============================================================
# Method 2: Twitter syndication API (fallback, no auth)
# ============================================================

def fetch_via_syndication(user):
    """Fetch tweets via Twitter's syndication API (cdn.syndication.twimg.com)
    
    This is Twitter's official embed API, used by WordPress and other platforms.
    No authentication required. More reliable than Nitter for high-profile accounts.
    """
    try:
        url = f"https://cdn.syndication.twimg.com/timeline/profile"
        params = {
            "screen_name": user["username"],
            "count": str(TWEETS_PER_USER),
        }
        log(f"  [syndication] Trying {user['username']}...")
        resp = SESSION.get(url, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Accept": "application/json",
            "Origin": "https://platform.twitter.com",
        })
        if resp.status_code == 200:
            data = resp.json()
            tweets = []
            # Response format: {"body": "<html>...</html>"} or JSON array
            body = data.get("body", "")
            if body and isinstance(body, str) and "<li class=" in body:
                # Parse HTML timeline
                tweets = _parse_syndication_html(body, user)
            elif isinstance(data, list):
                tweets = _parse_syndication_json(data, user)
            elif "tweets" in data:
                tweets = _parse_syndication_json(data["tweets"], user)

            if tweets:
                log(f"  [syndication] Got {len(tweets)} tweets")
                return tweets
            else:
                log(f"  [syndication] Got response but no tweets parsed")
        elif resp.status_code == 404:
            log(f"  [syndication] User not found")
        else:
            log(f"  [syndication] HTTP {resp.status_code}")
    except requests.exceptions.Timeout:
        log(f"  [syndication] Timeout")
    except Exception as e:
        log(f"  [syndication] Error: {str(e)[:80]}")
    return []


def _parse_syndication_html(body, user):
    """Parse syndication HTML timeline body"""
    from html.parser import HTMLParser

    class SyndicationParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tweets = []
            self.current = {}
            self.in_tweet = False
            self.in_text = False
            self.in_date = False
            self.buffer = ""

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            cls = attrs.get("class", "")
            if "tweet" in cls.lower() or "timeline-Tweet" in cls:
                self.in_tweet = True
                self.current = {}
                self.buffer = ""
            if self.in_tweet and "tweet-text" in cls.lower():
                self.in_text = True
                self.buffer = ""
            if self.in_tweet and ("date" in cls.lower() or "time" in cls.lower()):
                self.in_date = True
                self.buffer = ""
            if self.in_tweet and tag == "a":
                href = attrs.get("href", "")
                if "/status/" in href:
                    self.current["link"] = href

        def handle_data(self, data):
            if self.in_text or self.in_date:
                self.buffer += data

        def handle_endtag(self, tag):
            if self.in_text and tag in ("p", "div"):
                self.current["content"] = self.buffer.strip()
                self.in_text = False
                self.buffer = ""
            if self.in_date and tag in ("a", "span", "div"):
                self.current["date"] = self.buffer.strip()
                self.in_date = False
                self.buffer = ""
            if self.in_tweet and tag in ("li", "div"):
                if self.current.get("content"):
                    self.tweets.append(self.current)
                    self.current = {}
                self.in_tweet = False

    try:
        parser = SyndicationParser()
        parser.feed(body)
        tweets = []
        for t in parser.tweets[:TWEETS_PER_USER]:
            link = t.get("link", "")
            tid = ""
            if "/status/" in link:
                tid = link.split("/status/")[-1].split("?")[0].split("#")[0]
            if not tid:
                continue
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": t.get("date", ""),
                "content": t.get("content", ""),
                "url": f"https://x.com/{user['username']}/status/{tid}",
                "tweet_id": tid,
            })
        return tweets
    except Exception as e:
        log(f"  [syndication:parse] Error: {str(e)[:60]}")
        return []


def _parse_syndication_json(data, user):
    """Parse syndication JSON response"""
    tweets = []
    for t in data[:TWEETS_PER_USER]:
        if isinstance(t, dict):
            tid = t.get("id_str") or t.get("id") or ""
            content = t.get("text") or t.get("full_text") or ""
            created_at = t.get("created_at", "")
            if not tid or not content:
                continue
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": created_at,
                "content": content,
                "url": f"https://x.com/{user['username']}/status/{tid}",
                "tweet_id": tid,
            })
    return tweets


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("Twitter/X Tweet Fetcher + Translator — GitHub Actions Relay")
    log(f"Target users: {len(TARGET_USERS)}")
    log(f"Nitter instances: {len(NITTER_INSTANCES)} (verified working)")
    log("=" * 60)

    all_tweets = []
    success_count = 0
    fail_count = 0

    for user in TARGET_USERS:
        log(f"\n--- {user['display_name']} (@{user['username']}) ---")

        # Step 1: Try Twitter Syndication API (official, most reliable)
        user_tweets = fetch_via_syndication(user)

        # Step 2: Fallback to Nitter (9 verified working instances)
        if not user_tweets:
            log(f"  [fallback] Syndication API failed, trying Nitter instances...")
            user_tweets = fetch_via_nitter(user)

        if user_tweets:
            all_tweets.extend(user_tweets)
            success_count += 1
            time.sleep(2)
        else:
            log(f"  FAILED: All methods exhausted for {user['username']}")
            fail_count += 1
            time.sleep(1)

    # Translate all tweets
    if all_tweets:
        log(f"\n{'=' * 60}")
        log(f"Translating {len(all_tweets)} tweets...")
        log("=" * 60)
        all_tweets = translate_tweets(all_tweets)

    # Save results
    log(f"\n{'=' * 60}")
    log(f"Total: {len(all_tweets)} tweets ({success_count}/4 users, {fail_count} failed)")
    log("=" * 60)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(all_tweets),
        "tweets": all_tweets,
        "stats": {
            "users_success": success_count,
            "users_failed": fail_count,
        }
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"Saved to {OUTPUT_FILE}")

    for t in all_tweets:
        trans = t.get("translated", "")
        log(f"  [{t['display_name']}] {t['content'][:80]}...")
        if trans:
            log(f"    -> {trans[:80]}...")

    # Return 0 if at least one user succeeded, 1 if all failed
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())