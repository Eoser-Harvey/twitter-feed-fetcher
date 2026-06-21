"""
Twitter/X Tweet Fetcher — Multi-method approach
Runs in GitHub Actions (overseas IP) to bypass China network restrictions.

Methods (tried in order):
1. socialdata.tools API (if SOCIALDATA_API_KEY is set) — most reliable
2. Twitter guest API (bearer token + guest token) — no credentials needed
3. twikit (if TWITTER_AUTH_TOKEN + TWITTER_CT0 are set) — uses web client cookies

Output: tweets.json committed to repo
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

# === Configuration ===
TARGET_USERS = [
    {"username": "elonmusk", "display_name": "马斯克", "user_id": "44196397"},
    {"username": "cz_binance", "display_name": "CZ (赵长鹏)", "user_id": "902926941413453824"},
    {"username": "realDonaldTrump", "display_name": "特朗普", "user_id": "25073877"},
    {"username": "aleabitoreddit", "display_name": "Serenity (白毛股神)", "user_id": None},  # Will resolve
]

TWEETS_PER_USER = 3
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tweets.json")

# Twitter public bearer token (embedded in Twitter's web client JS, not a secret)
TWITTER_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# Common headers for Twitter API
TWITTER_HEADERS = {
    "authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "en",
}


def log(msg):
    """Print with timestamp"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ============================================================
# Method 1: socialdata.tools API
# ============================================================
def fetch_via_socialdata(user, api_key):
    """Fetch tweets using socialdata.tools API"""
    user_id = user["user_id"]
    if not user_id:
        log(f"  [socialdata] No user_id for {user['username']}, trying to resolve...")
        # Try to get user profile first
        try:
            resp = requests.get(
                f"https://api.socialdata.tools/twitter/profile/{user['username']}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=30
            )
            if resp.status_code == 200:
                profile = resp.json()
                user_id = profile.get("id_str") or str(profile.get("id"))
                user["user_id"] = user_id
                log(f"  [socialdata] Resolved {user['username']} -> user_id={user_id}")
            else:
                log(f"  [socialdata] Failed to resolve user: {resp.status_code}")
                return []
        except Exception as e:
            log(f"  [socialdata] Error resolving user: {e}")
            return []

    try:
        resp = requests.get(
            f"https://api.socialdata.tools/twitter/user/{user_id}/tweets",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            tweets = data.get("tweets", [])
            log(f"  [socialdata] Got {len(tweets)} tweets for {user['username']}")
            return parse_socialdata_tweets(tweets, user)
        elif resp.status_code == 402:
            log(f"  [socialdata] Insufficient balance (402)")
            return []
        else:
            log(f"  [socialdata] Error: {resp.status_code} - {resp.text[:200]}")
            return []
    except Exception as e:
        log(f"  [socialdata] Exception: {e}")
        return []


def parse_socialdata_tweets(tweets_raw, user):
    """Parse socialdata.tools tweet format to standard format"""
    tweets = []
    for t in tweets_raw[:TWEETS_PER_USER]:
        # Skip replies (only want original tweets)
        if t.get("in_reply_to_status_id_str"):
            continue
        tweet = {
            "id": f"tweet_{user['username']}_{t['id_str']}",
            "username": user["username"],
            "display_name": user["display_name"],
            "published_at": t.get("tweet_created_at", ""),
            "content": t.get("full_text") or t.get("text") or "",
            "url": f"https://x.com/{user['username']}/status/{t['id_str']}",
            "tweet_id": t["id_str"],
        }
        tweets.append(tweet)
    return tweets[:TWEETS_PER_USER]


# ============================================================
# Method 2: Twitter Guest API (no credentials needed)
# ============================================================
def get_guest_token():
    """Get a guest token from Twitter API"""
    try:
        resp = requests.post(
            "https://api.twitter.com/1.1/guest/activate.json",
            headers={
                "authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
                "user-agent": TWITTER_HEADERS["user-agent"],
            },
            timeout=15
        )
        if resp.status_code == 200:
            token = resp.json().get("guest_token")
            log(f"  [guest] Got guest token: {token[:20]}...")
            return token
        else:
            log(f"  [guest] Failed to get guest token: {resp.status_code}")
            return None
    except Exception as e:
        log(f"  [guest] Exception getting guest token: {e}")
        return None


def fetch_via_guest_api(user, guest_token):
    """Fetch tweets using Twitter guest API"""
    user_id = user["user_id"]
    if not user_id:
        log(f"  [guest] No user_id for {user['username']}, skipping")
        return []

    headers = TWITTER_HEADERS.copy()
    headers["x-guest-token"] = guest_token

    # Try timeline endpoint
    try:
        params = {
            "include_tweet_replies": "false",
            "include_tweet_stats": "true",
            "include_user_entities": "true",
            "include_promoted_content": "false",
            "count": str(TWEETS_PER_USER * 2),
        }
        resp = requests.get(
            f"https://api.twitter.com/2/timeline/profile/{user_id}.json",
            headers=headers,
            params=params,
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            tweets = parse_twitter_timeline(data, user)
            log(f"  [guest] Got {len(tweets)} tweets for {user['username']}")
            return tweets
        else:
            log(f"  [guest] Timeline error: {resp.status_code}")
            # Try search/adaptive as fallback
            return fetch_via_guest_search(user, guest_token)
    except Exception as e:
        log(f"  [guest] Exception: {e}")
        return []


def fetch_via_guest_search(user, guest_token):
    """Fetch tweets using Twitter search/adaptive endpoint"""
    headers = TWITTER_HEADERS.copy()
    headers["x-guest-token"] = guest_token

    try:
        params = {
            "q": f"from:{user['username']}",
            "count": str(TWEETS_PER_USER * 2),
            "query_source": "typed_query",
            "pc": "1",
            "spelling_corrections": "0",
        }
        resp = requests.get(
            "https://api.twitter.com/2/search/adaptive.json",
            headers=headers,
            params=params,
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            tweets = parse_twitter_search(data, user)
            log(f"  [guest-search] Got {len(tweets)} tweets for {user['username']}")
            return tweets
        else:
            log(f"  [guest-search] Error: {resp.status_code}")
            return []
    except Exception as e:
        log(f"  [guest-search] Exception: {e}")
        return []


def parse_twitter_timeline(data, user):
    """Parse Twitter timeline API response"""
    tweets = []
    tweets_data = data.get("globalObjects", {}).get("tweets", {})
    users_data = data.get("globalObjects", {}).get("users", {})

    # Sort by created_at descending
    sorted_tweets = sorted(tweets_data.values(), key=lambda t: t.get("created_at", 0), reverse=True)

    for t in sorted_tweets:
        # Skip replies and retweets
        if t.get("in_reply_to_status_id_str"):
            continue
        if t.get("retweeted_status_id_str"):
            continue

        tweet = {
            "id": f"tweet_{user['username']}_{t['id_str']}",
            "username": user["username"],
            "display_name": user["display_name"],
            "published_at": datetime.fromtimestamp(t["created_at"], tz=timezone.utc).isoformat(),
            "content": t.get("full_text") or t.get("text") or "",
            "url": f"https://x.com/{user['username']}/status/{t['id_str']}",
            "tweet_id": t["id_str"],
        }
        tweets.append(tweet)
        if len(tweets) >= TWEETS_PER_USER:
            break

    return tweets


def parse_twitter_search(data, user):
    """Parse Twitter search/adaptive API response"""
    tweets = []
    tweets_data = data.get("globalObjects", {}).get("tweets", {})

    # Sort by created_at descending
    sorted_tweets = sorted(tweets_data.values(), key=lambda t: t.get("created_at", 0), reverse=True)

    for t in sorted_tweets:
        # Only include tweets from the target user
        if str(t.get("user_id", "")) != str(user.get("user_id", "")):
            # Check by username in the user object
            user_obj = data.get("globalObjects", {}).get("users", {}).get(str(t.get("user_id", "")), {})
            if user_obj.get("screen_name", "").lower() != user["username"].lower():
                continue

        # Skip replies
        if t.get("in_reply_to_status_id_str"):
            continue

        tweet = {
            "id": f"tweet_{user['username']}_{t['id_str']}",
            "username": user["username"],
            "display_name": user["display_name"],
            "published_at": datetime.fromtimestamp(t["created_at"], tz=timezone.utc).isoformat(),
            "content": t.get("full_text") or t.get("text") or "",
            "url": f"https://x.com/{user['username']}/status/{t['id_str']}",
            "tweet_id": t["id_str"],
        }
        tweets.append(tweet)
        if len(tweets) >= TWEETS_PER_USER:
            break

    return tweets


# ============================================================
# Method 3: twikit (web client cookies)
# ============================================================
def fetch_via_twikit(user, auth_token, ct0):
    """Fetch tweets using twikit library"""
    try:
        from twikit import Client
    except ImportError:
        log("  [twikit] twikit not installed, skipping")
        return []

    try:
        client = Client("en-US")
        client._session.cookies.set("auth_token", auth_token, domain=".x.com")
        client._session.cookies.set("ct0", ct0, domain=".x.com")
        client._set_csrf_token(ct0)

        user_id = user["user_id"]
        if not user_id:
            # Search for user
            results = client.search_user(user["username"])
            if results:
                user_id = results[0].id
                user["user_id"] = user_id
            else:
                log(f"  [twikit] User not found: {user['username']}")
                return []

        tweets = client.get_user_tweets(user_id, tweet_type="Tweets", count=TWEETS_PER_USER * 2)
        parsed = []
        for t in tweets[:TWEETS_PER_USER]:
            parsed.append({
                "id": f"tweet_{user['username']}_{t.id}",
                "username": user["username"],
                "display_name": user["display_name"],
                "published_at": t.created_at.isoformat() if hasattr(t.created_at, 'isoformat') else str(t.created_at),
                "content": t.text,
                "url": f"https://x.com/{user['username']}/status/{t.id}",
                "tweet_id": t.id,
            })
        log(f"  [twikit] Got {len(parsed)} tweets for {user['username']}")
        return parsed
    except Exception as e:
        log(f"  [twikit] Exception: {e}")
        return []


# ============================================================
# Method 4: syndication API (fetch by tweet ID, accessible from China)
# ============================================================
def fetch_via_syndication(tweet_id):
    """Fetch a single tweet via syndication API (accessible from China)"""
    try:
        resp = requests.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=0",
            headers={"User-Agent": TWITTER_HEADERS["user-agent"]},
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            log(f"  [syndication] Error for {tweet_id}: {resp.status_code}")
            return None
    except Exception as e:
        log(f"  [syndication] Exception: {e}")
        return None


# ============================================================
# Main fetcher
# ============================================================
def fetch_all_tweets():
    """Fetch tweets for all target users using available methods"""
    all_tweets = []
    socialdata_key = os.environ.get("SOCIALDATA_API_KEY", "")
    twikit_auth_token = os.environ.get("TWITTER_AUTH_TOKEN", "")
    twikit_ct0 = os.environ.get("TWITTER_CT0", "")

    # Determine which methods are available
    methods = []
    if socialdata_key:
        methods.append("socialdata")
    methods.append("guest")  # Always try guest API
    if twikit_auth_token and twikit_ct0:
        methods.append("twikit")

    log(f"Available methods: {', '.join(methods)}")

    # Get guest token if needed
    guest_token = None
    if "guest" in methods:
        log("Getting Twitter guest token...")
        guest_token = get_guest_token()

    for user in TARGET_USERS:
        log(f"\n--- Fetching tweets for {user['display_name']} (@{user['username']}) ---")
        user_tweets = []

        for method in methods:
            log(f"  Trying method: {method}")

            if method == "socialdata":
                user_tweets = fetch_via_socialdata(user, socialdata_key)
            elif method == "guest":
                if guest_token:
                    user_tweets = fetch_via_guest_api(user, guest_token)
                else:
                    log("  [guest] No guest token available")
                    continue
            elif method == "twikit":
                user_tweets = fetch_via_twikit(user, twikit_auth_token, twikit_ct0)

            if user_tweets:
                log(f"  ✓ Method '{method}' succeeded with {len(user_tweets)} tweets")
                break
            else:
                log(f"  ✗ Method '{method}' returned no tweets")
                time.sleep(2)  # Rate limit courtesy

        if not user_tweets:
            log(f"  ⚠ No tweets fetched for {user['username']} from any method")

        all_tweets.extend(user_tweets)
        time.sleep(2)  # Rate limit courtesy

    return all_tweets


def main():
    log("=" * 60)
    log("Twitter/X Tweet Fetcher — GitHub Actions Relay")
    log(f"Time: {datetime.now(timezone.utc).isoformat()}")
    log(f"Target users: {len(TARGET_USERS)}")
    log("=" * 60)

    tweets = fetch_all_tweets()

    log(f"\n{'=' * 60}")
    log(f"Total tweets fetched: {len(tweets)}")
    log("=" * 60)

    # Save to file
    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(tweets),
        "tweets": tweets,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"Saved to {OUTPUT_FILE}")

    # Print summary
    for t in tweets:
        log(f"  [{t['display_name']}] {t['content'][:80]}...")

    return 0 if tweets else 1


if __name__ == "__main__":
    sys.exit(main())
