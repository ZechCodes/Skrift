"""Blog hooks — real-time notification when posts are published."""

from skrift.lib.hooks import action, AFTER_PAGE_SAVE
from skrift.lib.notifications import notify_session, NotificationMode

_blog_session_nids: set[str] = set()


def register_blog_session(nid: str) -> None:
    """Track a session NID for blog real-time updates."""
    _blog_session_nids.add(nid)


@action(AFTER_PAGE_SAVE, priority=20)
async def notify_new_post(page, *, is_new=False, **kwargs):
    """Send a timeseries notification when a post is published."""
    if page.type != "post" or not page.is_published:
        return

    for nid in _blog_session_nids:
        await notify_session(
            nid,
            "new_post",
            mode=NotificationMode.TIMESERIES,
            slug=page.slug,
            title=page.title,
            published_at=page.published_at.isoformat() if page.published_at else "",
            meta_description=page.meta_description or "",
            group=f"post:{page.slug}",
        )
