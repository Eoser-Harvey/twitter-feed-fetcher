"""
Twitter/X Tweet Fetcher — GitHub Actions Overseas Relay
Uses Nitter instances as proxy to bypass Twitter rate limits and login walls.

Strategy (in order):
1. Nitter instances (public, no auth needed) - fastest
2. FxTwitter API (single tweet, no auth) - for individual tweets
3. Twitter API (bearer token) - fallback
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

# Nitter instances (tried in order)
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

# Twitter API bearer token (public, from Twitter web client)
BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ============================================================
# Method 1: Nitter (RSS-like, no auth needed)
# ============================================================
def fetch_via_nitter(user):
    """Fetch tweets via Nitter frontend"""
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{user['username']}/rss"
            log(f"  [nitter] Trying {url}...")
            resp = SESSION.get(url, timeout=20, allow_redirects=True)
            if resp.status_code == 200 and "rss" in resp.headers.get("content-type", "").lower():
                tweets = parse_nitter_rss(resp.text, user)
                if tweets:
                    log(f"  [nitter] Got {len(tweets)} tweets from {instance}")
                    return tweets
            elif resp.status_code == 200:
                # Try parsing as HTML
                tweets = parse_nitter_html(resp.text, user, instance)
                if tweets:
                    log(f"  [nitter:html] Got {len(tweets)} tweets from {instance}")
                    return tweets
            else:
                log(f"  [nitter] {instance} returned {resp.status_code}")
        except Exception as e:
            log(f"  [nitter] {instance} error: {e}")
            continue

    log(f"  [nitter] All instances failed for {user['username']}")
    return []


def parse_nitter_rss(rss_text, user):
    """Parse Nitter RSS feed"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(rss_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        tweets = []
        for item in root.findall(".//item")[:TWEETS_PER_USER]:
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")
            description = item.find("description")

            title_text = title.text if title is not None else ""
            link_text = link.text if link is not None else ""
            pub_date_text = pub_date.text if pub_date is not None else ""
            desc_text = description.text if description is not None else ""

            # Clean the title (remove username prefix)
            if title_text.startswith(f"{user['display_name']}:"):
                content = title_text[len(f"{user['display_name']}:"):].strip()
            elif title_text.startswith(f"@"):
                content = title_text.split(":", 1)[-1].strip() if ":" in title_text else title_text
            else:
                content = title_text

            # Extract tweet ID from link
            tweet_id = link_text.rstrip("/").split("/")[-1] if link_text else ""

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
    except Exception as e:
        log(f"  [nitter:rss] Parse error: {e}")
        return []


def parse_nitter_html(html_text, user, instance):
    """Parse Nitter HTML page"""
    from html.parser import HTMLParser

    class TweetParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tweets = []
            self.current = {}
            self.in_tweet = False
            self.in_content = False
            self.in_link = False
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
            link = t.get("link", "")
            if link and not link.startswith("http"):
                link = instance.rstrip("/") + link
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
        log(f"  [nitter:html] Parse error: {e}")
        return []


# ============================================================
# Method 2: FxTwitter API (single tweet, no auth)
# ============================================================
def fetch_via_fxtwitter(tweet_id):
    """Fetch a single tweet via FxTwitter API"""
    try:
        url = f"https://api.fxtwitter.com/status/{tweet_id}"
        resp = SESSION.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            tweet = data.get("tweet", {})
            return {
                "text": tweet.get("text", ""),
                "author": tweet.get("author", {}).get("screen_name", ""),
                "likes": tweet.get("likes", 0),
                "retweets": tweet.get("retweets", 0),
                "views": tweet.get("views", 0),
            }
    except Exception as e:
        log(f"  [fxtwitter] Error: {e}")
    return None


# ============================================================
# Method 3: Twitter API (bearer token + guest token)
# ============================================================
def get_guest_token():
    """Get a guest token from Twitter"""
    try:
        resp = SESSION.post(
            "https://api.twitter.com/1.1/guest/activate.json",
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json().get("guest_token")
        log(f"  [twitter] Guest token: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"  [twitter] Guest token error: {e}")
    return None


def fetch_via_twitter_api(user, guest_token):
    """Fetch tweets via Twitter API"""
    if not guest_token:
        return []

    headers = {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "x-guest-token": guest_token,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "User-Agent": HEADERS["User-Agent"],
    }

    try:
        # Try UserTweets endpoint
        variables = {
            "userId": user.get("rest_id", ""),
            "count": TWEETS_PER_USER * 2,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        features = {
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
        }

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(features),
        }

        resp = SESSION.get(
            "https://twitter.com/i/api/graphql/resolve/UserTweets",
            headers=headers,
            params=params,
            timeout=30
        )

        if resp.status_code == 200:
            data = resp.json()
            tweets = extract_tweets_from_graphql(data, user)
            if tweets:
                log(f"  [twitter] Got {len(tweets)} tweets via GraphQL")
                return tweets
        else:
            log(f"  [twitter] GraphQL: {resp.status_code}")
    except Exception as e:
        log(f"  [twitter] GraphQL error: {e}")

    return []


def extract_tweets_from_graphql(data, user):
    """Extract tweets from Twitter GraphQL response"""
    tweets = []
    try:
        instructions = []
        # Navigate the nested structure
        if "data" in data:
            data = data["data"]
        if "user" in data:
            data = data["user"]
        if "result" in data:
            data = data["result"]
        if "timeline_v2" in data:
            data = data["timeline_v2"]
        if "timeline" in data:
            data = data["timeline"]
        if "instructions" in data:
            instructions = data["instructions"]

        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                content = entry.get("content", {})
                if content.get("entryType") != "TimelineTimelineItem":
                    continue
                item = content.get("itemContent", {})
                if item.get("itemType") != "TimelineTweet":
                    continue
                result = item.get("tweet_results", {}).get("result", {})
                # Handle retweet
                if "retweeted_status_result" in result:
                    result = result["retweeted_status_result"].get("result", {})

                legacy = result.get("legacy", {})
                if not legacy:
                    continue

                # Skip replies
                if legacy.get("in_reply_to_status_id_str"):
                    continue

                rest_id = result.get("rest_id", "")
                full_text = legacy.get("full_text", "")
                created_at = legacy.get("created_at", "")

                tweets.append({
                    "id": f"tweet_{user['username']}_{rest_id}",
                    "username": user["username"],
                    "display_name": user["display_name"],
                    "published_at": created_at,
                    "content": full_text,
                    "url": f"https://x.com/{user['username']}/status/{rest_id}",
                    "tweet_id": rest_id,
                })
                if len(tweets) >= TWEETS_PER_USER:
                    break
            if len(tweets) >= TWEETS_PER_USER:
                break
    except Exception as e:
        log(f"  [twitter] Extract error: {e}")

    return tweets


# ============================================================
# Method 4: Twitter API v2 search (if bearer token works)
# ============================================================
def fetch_search_tweets(user):
    """Fetch tweets from Twitter search (no auth needed for some endpoints)"""
    try:
        query = f"from:{user['username']}"
        url = f"https://api.twitter.com/2/search/adaptive.json?q={quote(query)}&count=20&tweet_mode=extended"
        headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
        resp = SESSION.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            tweets = extract_search_tweets(data, user)
            if tweets:
                log(f"  [search] Got {len(tweets)} tweets")
                return tweets
        else:
            log(f"  [search] {resp.status_code}")
    except Exception as e:
        log(f"  [search] Error: {e}")
    return []


def extract_search_tweets(data, user):
    """Extract tweets from search API response"""
    tweets = []
    try:
        global_objects = data.get("globalObjects", {})
        tweets_data = global_objects.get("tweets", {})
        users_data = global_objects.get("users", {})

        sorted_tweets = sorted(
            tweets_data.values(),
            key=lambda t: t.get("created_at", "0"),
            reverse=True
        )

        for t in sorted_tweets:
            if t.get("in_reply_to_status_id_str"):
                continue
            tid = t.get("id_str", "")
            full_text = t.get("full_text", "")
            created_at = t.get("created_at", "")

            tweets.append({
                "id": f"tweet_{user['username']}_{tid}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": created_at,
                "content": full_text,
                "url": f"https://x.com/{user['username']}/status/{tid}",
                "tweet_id": tid,
            })
            if len(tweets) >= TWEETS_PER_USER:
                break
    except Exception as e:
        log(f"  [search] Extract error: {e}")
    return tweets


# ============================================================
# Main
# ============================================================
def main():
    log("=" * 60)
    log("Twitter/X Tweet Fetcher — GitHub Actions Relay")
    log(f"Target users: {len(TARGET_USERS)}")
    log("=" * 60)

    all_tweets = []
    guest_token = None

    for user in TARGET_USERS:
        log(f"\n--- {user['display_name']} (@{user['username']}) ---")
        user_tweets = []

        # Method 1: Nitter
        log("  Trying Nitter...")
        user_tweets = fetch_via_nitter(user)
        if user_tweets:
            all_tweets.extend(user_tweets)
            time.sleep(2)
            continue

        # Method 2: Twitter API
        log("  Trying Twitter API...")
        if not guest_token:
            guest_token = get_guest_token()
        user_tweets = fetch_via_twitter_api(user, guest_token)
        if user_tweets:
            all_tweets.extend(user_tweets)
            time.sleep(2)
            continue

        # Method 3: Search endpoint
        log("  Trying search endpoint...")
        user_tweets = fetch_search_tweets(user)
        if user_tweets:
            all_tweets.extend(user_tweets)
            time.sleep(2)
            continue

        log(f"  ⚠ All methods failed for {user['username']}")
        time.sleep(2)

    # Save results
    log(f"\n{'=' * 60}")
    log(f"Total: {len(all_tweets)} tweets")
    log("=" * 60)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(all_tweets),
        "tweets": all_tweets,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"Saved to {OUTPUT_FILE}")

    for t in all_tweets:
        log(f"  [{t['display_name']}] {t['content'][:80]}...")

    # Exit with error if no tweets (to signal failure)
    return 0 if all_tweets else 1


if __name__ == "__main__":
    sys.exit(main())