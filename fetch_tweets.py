"""
Twitter/X Tweet Fetcher with Translation — GitHub Actions Overseas Relay
v8: Auto-retry + Nitter instance self-healing (health check + Wiki auto-refresh)

Strategy per user:
  1. Try Syndication API (official, no auth)
  2. If fail → try Nitter RSS from healthy instances
  3. If both fail → wait 15s → retry dual-source once more
  4. Nitter instances: health-checked on startup, auto-refreshed from Wiki every 6h
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
import requests


def clean_tweet_id(tid):
    """Strip #m suffix from Nitter RSS tweet IDs to ensure consistent dedup."""
    return tid.replace("#m", "") if isinstance(tid, str) else str(tid)

# === Configuration ===
TARGET_USERS = [
    {"username": "elonmusk", "display_name": "马斯克"},
    {"username": "cz_binance", "display_name": "CZ (赵长鹏)"},
    {"username": "realDonaldTrump", "display_name": "特朗普"},
    {"username": "aleabitoreddit", "display_name": "Serenity (白毛股神)"},
    {"username": "qinbafrank", "display_name": "秦巴Frank"},
    {"username": "xiaomustock", "display_name": "小米股"},
    {"username": "xingpt", "display_name": "星Prompt"},
    {"username": "hibtc37", "display_name": "HiBTC"},
]
TWEETS_PER_USER = 3
RETRY_DELAY_SECONDS = 15       # Wait between retry attempts
MAX_RETRY_ATTEMPTS = 2         # Total attempts per user (1 initial + 1 retry)
WIKI_REFRESH_HOURS = 6         # Re-scrape Wiki every 6 hours
HEALTH_CHECK_TIMEOUT = 5       # Seconds for instance health check

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "tweets.json")
TRANSLATION_CACHE_FILE = os.path.join(BASE_DIR, "translation_cache.json")
NITTER_CACHE_FILE = os.path.join(BASE_DIR, "nitter_instances.json")
WIKI_URL = "https://github.com/zedeus/nitter/wiki/Instances"

# Default Nitter instances (used if cache unavailable & Wiki unreachable)
DEFAULT_NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacyredirect.com",
    "https://lightbrd.com",
    "https://nitter.space",
    "https://nitter.tiekoetter.com",
    "https://nuku.trabun.org",
    "https://nitter.catsarch.com",
    "https://nitter.kareem.one",
    "https://nitter.net",
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
# Nitter Instance Manager — self-healing (health check + Wiki refresh)
# ============================================================

def load_nitter_cache():
    """Load cached Nitter instances and health data"""
    if os.path.exists(NITTER_CACHE_FILE):
        try:
            with open(NITTER_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_nitter_cache(cache):
    """Save Nitter instances cache to file"""
    try:
        data = {
            "instances": cache.get("instances", []),
            "healthy": cache.get("healthy", []),
            "unhealthy": cache.get("unhealthy", []),
            "last_wiki_refresh": cache.get("last_wiki_refresh", ""),
            "last_health_check": cache.get("last_health_check", ""),
        }
        with open(NITTER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  [nitter cache] Save error: {e}")


def scrape_wiki_instances():
    """Scrape official Nitter Wiki for currently listed public instances"""
    log("  [wiki] Fetching latest instance list from GitHub Wiki...")
    try:
        resp = SESSION.get(WIKI_URL, timeout=20)
        if resp.status_code != 200:
            log(f"  [wiki] HTTP {resp.status_code}, using cached/fallback list")
            return None

        text = resp.text
        # Extract URLs where BOTH Online and Working are checked
        pattern = re.compile(
            r'\|\s*\[([^\]]+)\]\(https?://([^/\)]+)/?\)\s*\|'
            r'\s*✅\s*\|'
            r'\s*✅\s*\|',
        )
        matches = pattern.findall(text)
        instances = []
        seen = set()
        for name, domain in matches:
            url = f"https://{domain}"
            if url not in seen:
                seen.add(url)
                instances.append(url)

        if instances:
            log(f"  [wiki] Found {len(instances)} working instances")
            if "https://nitter.net" not in seen:
                instances.append("https://nitter.net")
            return instances
    except Exception as e:
        log(f"  [wiki] Scrape error: {str(e)[:80]}")

    return None


def health_check_instances(instances):
    """Quick connectivity check: which Nitter instances respond?"""
    healthy = []
    unhealthy = []

    for instance in instances:
        try:
            check_url = f"{instance}/elonmusk"
            resp = SESSION.get(check_url, timeout=HEALTH_CHECK_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                healthy.append(instance)
                log(f"  [health] OK {instance}")
            elif resp.status_code in (429, 403):
                healthy.append(instance)
                log(f"  [health] WARN {instance} (HTTP {resp.status_code}, alive)")
            else:
                unhealthy.append(instance)
                log(f"  [health] DEAD {instance} (HTTP {resp.status_code})")
        except requests.exceptions.Timeout:
            unhealthy.append(instance)
            log(f"  [health] DEAD {instance} (timeout)")
        except requests.exceptions.ConnectionError:
            unhealthy.append(instance)
            log(f"  [health] DEAD {instance} (connection error)")
        except Exception as e:
            unhealthy.append(instance)
            log(f"  [health] DEAD {instance} ({str(e)[:40]})")

    return healthy, unhealthy


def needs_wiki_refresh(cache):
    """Check if Wiki should be re-scraped"""
    if not cache:
        return True
    last_refresh = cache.get("last_wiki_refresh", "")
    if not last_refresh:
        return True
    try:
        last_dt = datetime.fromisoformat(last_refresh)
        hours_passed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return hours_passed >= WIKI_REFRESH_HOURS
    except Exception:
        return True


def get_healthy_instances():
    """
    Main entry point for Nitter instance management.
    Self-healing: health-check existing, auto-refresh from Wiki every 6h.
    Returns list of healthy instances only.
    """
    log("\n[Nitter Instance Manager]")
    cache = load_nitter_cache()

    instances = None
    if needs_wiki_refresh(cache):
        log("  [wiki] Refreshing instance list from Wiki...")
        instances = scrape_wiki_instances()
        if instances:
            healthy, unhealthy = health_check_instances(instances)
            cache = {
                "instances": instances,
                "healthy": healthy,
                "unhealthy": unhealthy,
                "last_wiki_refresh": datetime.now(timezone.utc).isoformat(),
                "last_health_check": datetime.now(timezone.utc).isoformat(),
            }
            save_nitter_cache(cache)
        elif cache:
            log("  [wiki] Scrape failed, using cached instances")
        else:
            log("  [wiki] Scrape failed, using built-in fallback list")
            instances = DEFAULT_NITTER_INSTANCES
            healthy, unhealthy = health_check_instances(instances)
            cache = {
                "instances": instances,
                "healthy": healthy,
                "unhealthy": unhealthy,
                "last_wiki_refresh": "",
                "last_health_check": datetime.now(timezone.utc).isoformat(),
            }
            save_nitter_cache(cache)
    else:
        instances = cache.get("instances", DEFAULT_NITTER_INSTANCES)
        log("  [cache] Using cached instances (Wiki refresh not needed)")
        healthy, unhealthy = health_check_instances(instances)
        cache["healthy"] = healthy
        cache["unhealthy"] = unhealthy
        cache["last_health_check"] = datetime.now(timezone.utc).isoformat()
        save_nitter_cache(cache)

    if cache:
        healthy = cache.get("healthy", instances or DEFAULT_NITTER_INSTANCES)
    else:
        healthy = instances or DEFAULT_NITTER_INSTANCES

    if not healthy:
        log("  [health] WARNING: ALL instances unhealthy! Fallback to full list.")
        healthy = instances or DEFAULT_NITTER_INSTANCES

    log(f"  [result] {len(healthy)} healthy / {len(cache.get('unhealthy', [])) if cache else 0} unhealthy")
    return healthy


# ============================================================
# Translation
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
# Method 1: Nitter RSS
# ============================================================

def fetch_via_nitter(user, nitter_instances):
    for i, instance in enumerate(nitter_instances):
        try:
            rss_url = f"{instance}/{user['username']}/rss"
            log(f"  [nitter {i+1}/{len(nitter_instances)}] {instance}...")
            resp = SESSION.get(rss_url, timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "").lower()
                if "xml" in content_type or "rss" in content_type or resp.text.strip().startswith("<?xml"):
                    tweets = parse_nitter_rss(resp.text, user)
                    if tweets:
                        log(f"  [nitter RSS] Got {len(tweets)} tweets from {instance}")
                        return tweets
                if "<html" in resp.text.lower()[:200]:
                    tweets = parse_nitter_html(resp.text, user)
                    if tweets:
                        log(f"  [nitter HTML] Got {len(tweets)} tweets from {instance}")
                        return tweets
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
        time.sleep(0.5)
    return []


def parse_nitter_rss(rss_text, user):
    import xml.etree.ElementTree as ET
    try:
        rss_text = rss_text.strip()
        if not rss_text.startswith("<"):
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
            content = title_text
            if content.startswith(f"{user['display_name']}:"):
                content = content[len(f"{user['display_name']}:"):].strip()
            elif content.startswith("@"):
                parts = content.split(":", 1)
                content = parts[-1].strip() if len(parts) > 1 else content
            if not content:
                continue
            tweet_id = clean_tweet_id(link_text.rstrip("/").split("/")[-1]) if link_text else ""
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
                        tid = clean_tweet_id(href.rstrip("/").split("/")[-1])
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
# Method 2: Twitter syndication API
# ============================================================

def fetch_via_syndication(user):
    try:
        url = "https://cdn.syndication.twimg.com/timeline/profile"
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
            body = data.get("body", "")
            if body and isinstance(body, str) and "<li class=" in body:
                tweets = _parse_syndication_html(body, user)
            elif isinstance(data, list):
                tweets = _parse_syndication_json(data, user)
            elif "tweets" in data:
                tweets = _parse_syndication_json(data["tweets"], user)
            else:
                log(f"  [syndication] Unexpected response format")
                return []
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
# Dual-source fetch with auto-retry (v8)
# ============================================================

def fetch_user_tweets_with_retry(user, nitter_instances):
    """
    Attempt 1: Syndication → Nitter
    Attempt 2 (if both fail): wait RETRY_DELAY → Syndication → Nitter
    Returns (tweets_list, source_label, attempts_used)
    """
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        if attempt > 1:
            log(f"  [retry] Attempt {attempt}/{MAX_RETRY_ATTEMPTS} after {RETRY_DELAY_SECONDS}s delay...")
            time.sleep(RETRY_DELAY_SECONDS)

        syn_tweets = fetch_via_syndication(user)
        if syn_tweets:
            return syn_tweets, "Syndication API", attempt

        nit_tweets = fetch_via_nitter(user, nitter_instances)
        if nit_tweets:
            return nit_tweets, f"Nitter ({len(nitter_instances)} instances)", attempt

        if attempt < MAX_RETRY_ATTEMPTS:
            log(f"  [retry] Both sources failed on attempt {attempt}, will retry...")

    return [], "none", MAX_RETRY_ATTEMPTS


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("Twitter/X Tweet Fetcher + Translator — GitHub Actions Relay")
    log(f"v8: Auto-retry ({MAX_RETRY_ATTEMPTS} attempts) + Nitter self-healing")
    log(f"Target: {len(TARGET_USERS)} users, {TWEETS_PER_USER} tweets each")
    log("=" * 60)

    # Phase 0: Nitter instance health management
    healthy_instances = get_healthy_instances()

    # Phase 1: Fetch tweets with auto-retry
    all_tweets = []
    success_count = 0
    fail_count = 0

    for user in TARGET_USERS:
        log(f"\n--- {user['display_name']} (@{user['username']}) ---")
        user_tweets, source, attempts = fetch_user_tweets_with_retry(user, healthy_instances)
        if user_tweets:
            all_tweets.extend(user_tweets)
            success_count += 1
            log(f"  [OK] {len(user_tweets)} tweets via {source} (attempt {attempts}/{MAX_RETRY_ATTEMPTS})")
            time.sleep(2)
        else:
            log(f"  [FAIL] {user['username']} after {attempts} attempts")
            fail_count += 1
            time.sleep(1)

    # Phase 2: Translate
    if all_tweets:
        log(f"\n{'=' * 60}")
        log(f"Translating {len(all_tweets)} tweets...")
        log("=" * 60)
        all_tweets = translate_tweets(all_tweets)

    # Phase 3: Save
    log(f"\n{'=' * 60}")
    log(f"Result: {success_count}/4 users ({len(all_tweets)} tweets), {fail_count} failed")
    log("=" * 60)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(all_tweets),
        "tweets": all_tweets,
        "stats": {
            "users_success": success_count,
            "users_failed": fail_count,
            "nitter_healthy": len(healthy_instances),
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

    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
