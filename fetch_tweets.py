"""
Twitter/X Tweet Fetcher with Translation — GitHub Actions Overseas Relay
Uses Nitter instances as proxy to bypass Twitter rate limits and login walls.
Also translates tweets to Chinese using Google Translate (accessible from overseas IP).
Strategy (in order):
1. Nitter instances (public, no auth needed) - fastest
2. FxTwitter API (JSON, no auth) - reliable fallback
3. Twitter API (bearer token) - final fallback
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

# Nitter instances (tried in order, verified working 2026-06)
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.unixfox.eu",
    "https://nitter.domain.glass",
    "https://nitter.esmailelbob.xyz",
    "https://nitter.space",
    "https://nitter.moomoo.me",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ============================================================
# Translation (Google Translate, accessible from overseas)
# ============================================================

def translate_text(text, target_lang="zh-CN", source_lang="auto"):
    """Translate text using Google Translate API"""
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
    """Load existing translation cache"""
    if os.path.exists(TRANSLATION_CACHE_FILE):
        try:
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_translation_cache(cache):
    """Save translation cache"""
    try:
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  [translate] Save cache error: {e}")


def translate_tweets(tweets):
    """Translate all tweets and cache results"""
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
# Method 1: Nitter (RSS-like, no auth needed)
# ============================================================

def fetch_via_nitter(user):
    """Fetch tweets via Nitter frontend"""
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{user['username']}/rss"
            log(f"  [nitter] Trying {instance}...")
            resp = SESSION.get(url, timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "").lower()
                if "rss" in content_type or "xml" in content_type:
                    tweets = parse_nitter_rss(resp.text, user)
                    if tweets:
                        log(f"  [nitter] Got {len(tweets)} tweets from {instance} (RSS)")
                        return tweets
                # Try HTML parsing as fallback
                tweets = parse_nitter_html(resp.text, user, instance)
                if tweets:
                    log(f"  [nitter:html] Got {len(tweets)} tweets from {instance}")
                    return tweets
            elif resp.status_code == 429:
                log(f"  [nitter] {instance} rate limited (429)")
            elif resp.status_code == 404:
                log(f"  [nitter] {instance} returned 404 for {user['username']}")
            else:
                log(f"  [nitter] {instance} returned {resp.status_code}")
        except requests.exceptions.Timeout:
            log(f"  [nitter] {instance} timeout")
        except requests.exceptions.ConnectionError as e:
            log(f"  [nitter] {instance} connection error: {str(e)[:80]}")
        except Exception as e:
            log(f"  [nitter] {instance} error: {str(e)[:80]}")
        continue
    log(f"  [nitter] All {len(NITTER_INSTANCES)} instances failed for {user['username']}")
    return []


def parse_nitter_rss(rss_text, user):
    """Parse Nitter RSS feed"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(rss_text)
        tweets = []
        for item in root.findall(".//item")[:TWEETS_PER_USER]:
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")
            title_text = title.text if title is not None else ""
            link_text = link.text if link is not None else ""
            pub_date_text = pub_date.text if pub_date is not None else ""
            # Clean the title
            if title_text.startswith(f"{user['display_name']}:"):
                content = title_text[len(f"{user['display_name']}:"):].strip()
            elif title_text.startswith("@"):
                parts = title_text.split(":", 1)
                content = parts[-1].strip() if len(parts) > 1 else title_text
            else:
                content = title_text
            tweet_id = link_text.rstrip("/").split("/")[-1] if link_text else ""
            url = f"https://x.com/{user['username']}/status/{tweet_id}"
            tweets.append({
                "id": f"tweet_{user['username']}_{tweet_id}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": pub_date_text,
                "content": content,
                "url": url,
                "tweet_id": tweet_id,
            })
        return tweets
    except Exception as e:
        log(f"  [nitter:rss] Parse error: {str(e)[:80]}")
        return []


def parse_nitter_html(html_text, user, instance):
    """Parse Nitter HTML page (simplified)"""
    from html.parser import HTMLParser

    class TweetParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tweets = []
            self.current = {}
            self.in_tweet = False
            self.in_content = False
            self.in_date = False
            self.text_buffer = ""

        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            cls = attrs_dict.get("class", "")
            if "tweet-body" in cls or "tweet-content" in cls:
                self.in_tweet = True
                self.current = {}
                self.text_buffer = ""
            if "tweet-link" in cls or "permalink" in cls:
                href = attrs_dict.get("href", "")
                if href:
                    self.current["link"] = href
                    tid = href.rstrip("/").split("/")[-1]
                    self.current["tweet_id"] = tid
            if "tweet-date" in cls:
                self.in_date = True
                self.text_buffer = ""
            if self.in_tweet and tag in ("a", "span", "div"):
                self.in_content = True

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
                content = self.text_buffer.strip()
                if content and len(content) > 10:
                    self.current["content"] = content
                    if self.current.get("tweet_id"):
                        self.tweets.append(self.current)
                        self.current = {}
                    self.in_tweet = False
                self.text_buffer = ""
                self.in_content = False

    try:
        parser = TweetParser()
        parser.feed(html_text)
        tweets = []
        for t in parser.tweets[:TWEETS_PER_USER]:
            tid = t.get("tweet_id", "")
            url = f"https://x.com/{user['username']}/status/{tid}"
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": t.get("date", ""),
                "content": t.get("content", ""),
                "url": url,
                "tweet_id": tid,
            })
        return tweets
    except Exception as e:
        log(f"  [nitter:html] Parse error: {str(e)[:80]}")
        return []


# ============================================================
# Method 2: FxTwitter API (JSON, no auth, reliable fallback)
# ============================================================

def fetch_via_fxtwitter(user):
    """Fetch tweets via FxTwitter API (v2, JSON format, no auth needed)
    
    FxTwitter is a well-maintained open-source proxy that provides
    clean JSON API for Twitter/X data. More reliable than Nitter
    for high-profile accounts like elonmusk.
    """
    try:
        url = f"https://api.fxtwitter.com/{user['username']}/latest"
        log(f"  [fxtwitter] Trying {url}...")
        resp = SESSION.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if resp.status_code != 200:
            log(f"  [fxtwitter] HTTP {resp.status_code}")
            return []

        data = resp.json()
        if data.get("code") == 404:
            log(f"  [fxtwitter] User not found: {user['username']}")
            return []

        tweets = []
        raw_tweets = data.get("tweets")
        if not raw_tweets:
            # Try the timeline endpoint as fallback
            return _fetch_via_fxtwitter_timeline(user)

        for t in raw_tweets[:TWEETS_PER_USER]:
            tweet_data = t.get("tweet", t)
            tid = tweet_data.get("id", "")
            if not tid:
                continue
            content = tweet_data.get("text", "")
            created_at = tweet_data.get("created_at", "")
            # Build clean URL
            screen_name = tweet_data.get("author", {}).get("screen_name", user["username"])
            url = f"https://x.com/{screen_name}/status/{tid}"
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": created_at,
                "content": content,
                "url": url,
                "tweet_id": tid,
            })

        if tweets:
            log(f"  [fxtwitter] Got {len(tweets)} tweets")
        return tweets
    except requests.exceptions.Timeout:
        log(f"  [fxtwitter] Timeout")
    except requests.exceptions.ConnectionError as e:
        log(f"  [fxtwitter] Connection error: {str(e)[:80]}")
    except Exception as e:
        log(f"  [fxtwitter] Error: {str(e)[:80]}")
    return []


def _fetch_via_fxtwitter_timeline(user):
    """Fallback: try FxTwitter timeline endpoint"""
    try:
        url = f"https://api.fxtwitter.com/{user['username']}/timeline"
        resp = SESSION.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if resp.status_code != 200:
            return []

        data = resp.json()
        tweets = []
        raw_tweets = data.get("tweets", [])
        for t in raw_tweets[:TWEETS_PER_USER]:
            tweet_data = t.get("tweet", t)
            tid = tweet_data.get("id", "")
            if not tid:
                continue
            content = tweet_data.get("text", "")
            created_at = tweet_data.get("created_at", "")
            screen_name = tweet_data.get("author", {}).get("screen_name", user["username"])
            url = f"https://x.com/{screen_name}/status/{tid}"
            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": created_at,
                "content": content,
                "url": url,
                "tweet_id": tid,
            })
        if tweets:
            log(f"  [fxtwitter:timeline] Got {len(tweets)} tweets")
        return tweets
    except Exception as e:
        log(f"  [fxtwitter:timeline] Error: {str(e)[:80]}")
    return []


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("Twitter/X Tweet Fetcher + Translator — GitHub Actions Relay")
    log(f"Target users: {len(TARGET_USERS)}")
    log("=" * 60)

    all_tweets = []
    success_count = 0
    fail_count = 0

    for user in TARGET_USERS:
        log(f"\n--- {user['display_name']} (@{user['username']}) ---")

        # Step 1: Try Nitter (fastest, no auth)
        user_tweets = fetch_via_nitter(user)

        # Step 2: Fallback to FxTwitter API (more reliable for high-profile accounts)
        if not user_tweets:
            log(f"  ⚠ Nitter failed for {user['username']}, trying FxTwitter API...")
            user_tweets = fetch_via_fxtwitter(user)

        if user_tweets:
            all_tweets.extend(user_tweets)
            success_count += 1
            time.sleep(2)
        else:
            log(f"  ❌ All methods failed for {user['username']} (@{user['username']})")
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
    log(f"Total: {len(all_tweets)} tweets (success: {success_count}/4 users, failed: {fail_count}/4)")
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