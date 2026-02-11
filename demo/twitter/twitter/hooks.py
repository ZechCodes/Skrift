import re
from html import escape

from skrift.lib.hooks import filter as filter_hook

# Action hook constants
BEFORE_TWEET_SAVE = "before_tweet_save"
AFTER_TWEET_SAVE = "after_tweet_save"
BEFORE_TWEET_DELETE = "before_tweet_delete"
AFTER_TWEET_DELETE = "after_tweet_delete"
AFTER_TWEET_LIKE = "after_tweet_like"
AFTER_TWEET_UNLIKE = "after_tweet_unlike"
AFTER_USER_FOLLOW = "after_user_follow"
AFTER_USER_UNFOLLOW = "after_user_unfollow"

# Filter hook constants
TWEET_CONTENT_RENDER = "tweet_content_render"
TWEET_FEED_QUERY = "tweet_feed_query"
TWEET_SEO_META = "tweet_seo_meta"
TWEET_OG_META = "tweet_og_meta"

# URL pattern
_URL_RE = re.compile(r'(https?://[^\s<>"\']+)')
# @mention pattern
_MENTION_RE = re.compile(r'@(\w+)')
# #hashtag pattern
_HASHTAG_RE = re.compile(r'#(\w+)')


@filter_hook(TWEET_CONTENT_RENDER, priority=10)
def linkify_urls(content: str) -> str:
    """Escape HTML then convert URLs to clickable links."""
    content = escape(content)
    return _URL_RE.sub(r'<a href="\1" rel="nofollow noopener" target="_blank">\1</a>', content)


@filter_hook(TWEET_CONTENT_RENDER, priority=20)
def linkify_mentions(content: str) -> str:
    """Convert @username mentions to profile links."""
    return _MENTION_RE.sub(r'<a href="/profile/\1">@\1</a>', content)


@filter_hook(TWEET_CONTENT_RENDER, priority=30)
def linkify_hashtags(content: str) -> str:
    """Convert #hashtags to search links."""
    return _HASHTAG_RE.sub(r'<a href="/search?q=%23\1">#\1</a>', content)
