from pathlib import Path
from uuid import UUID
from datetime import datetime, UTC

from litestar import Controller, Request, get, post
from litestar.exceptions import NotFoundException, NotAuthorizedException
from litestar.response import Template as TemplateResponse, Redirect
from litestar.datastructures import FormMultiDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User
from skrift.db.models import PageType
from skrift.db.services import page_service
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


class WebController(Controller):
    path = "/"

    async def _get_user_context(
        self, request: "Request", db_session: AsyncSession
    ) -> dict:
        """Get user data for template context if logged in."""
        user_id = request.session.get("user_id")
        if not user_id:
            return {"user": None}

        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        return {"user": user}

    async def _require_auth(self, request: "Request", db_session: AsyncSession) -> User:
        """Require user authentication, raise exception if not logged in."""
        user_id = request.session.get("user_id")
        if not user_id:
            raise NotAuthorizedException("You must be logged in to access this page")

        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            raise NotAuthorizedException("Invalid user session")
        return user

    @get("/")
    async def index(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """Home page."""
        user_ctx = await self._get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        # Fetch published posts for display
        posts = await page_service.list_pages(
            db_session,
            page_type=PageType.POST,
            published_only=True,
            limit=10,
        )

        return TemplateResponse(
            "index.html",
            context={"flash": flash, "posts": posts, **user_ctx},
        )

    @get("/post/{slug:str}")
    async def view_post(
        self, request: "Request", db_session: AsyncSession, slug: str
    ) -> TemplateResponse:
        """View a single post by slug."""
        user_ctx = await self._get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        # Fetch post from database
        page = await page_service.get_page_by_slug(
            db_session, slug, published_only=not request.session.get("user_id")
        )
        if not page or page.type != PageType.POST:
            raise NotFoundException(f"Post '{slug}' not found")

        template = Template("post", slug, context={"slug": slug, "page": page})
        return template.render(TEMPLATE_DIR, flash=flash, **user_ctx)

    @get("/page/{path:path}")
    async def view_page(
        self, request: "Request", db_session: AsyncSession, path: str
    ) -> TemplateResponse:
        """View a page by path with WP-like template resolution."""
        user_ctx = await self._get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        # Split path into slugs (e.g., "services/web" -> ["services", "web"])
        slugs = [s for s in path.split("/") if s]

        # Use the full path as the slug for database lookup
        page_slug = "/".join(slugs)

        # Fetch page from database
        page = await page_service.get_page_by_slug(
            db_session, page_slug, published_only=not request.session.get("user_id")
        )
        if not page or page.type != PageType.PAGE:
            raise NotFoundException(f"Page '{path}' not found")

        template = Template("page", *slugs, context={"path": path, "slugs": slugs, "page": page})
        return template.render(TEMPLATE_DIR, flash=flash, **user_ctx)

    @get("/posts/new")
    async def new_post_page(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """Show new post form."""
        user = await self._require_auth(request, db_session)
        flash = request.session.pop("flash", None)

        return TemplateResponse(
            "posts/new.html",
            context={"flash": flash, "user": user},
        )

    @post("/posts/new")
    async def create_post(
        self, request: "Request", db_session: AsyncSession, data: FormMultiDict
    ) -> Redirect:
        """Create a new post."""
        user = await self._require_auth(request, db_session)

        title = data.get("title", "").strip()
        slug = data.get("slug", "").strip()
        content = data.get("content", "").strip()
        is_published = data.get("is_published") == "on"

        if not title or not slug:
            request.session["flash"] = "Title and slug are required"
            return Redirect(path="/posts/new")

        # Set published_at if publishing
        published_at = datetime.now(UTC) if is_published else None

        try:
            await page_service.create_page(
                db_session,
                slug=slug,
                title=title,
                content=content,
                page_type=PageType.POST,
                is_published=is_published,
                published_at=published_at,
                user_id=user.id,
            )
            request.session["flash"] = f"Post '{title}' created successfully!"
            return Redirect(path="/posts/my")
        except Exception as e:
            request.session["flash"] = f"Error creating post: {str(e)}"
            return Redirect(path="/posts/new")

    @get("/posts/my")
    async def my_posts(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """List user's posts."""
        user = await self._require_auth(request, db_session)
        flash = request.session.pop("flash", None)

        posts = await page_service.list_pages(
            db_session,
            page_type=PageType.POST,
            user_id=user.id,
        )

        return TemplateResponse(
            "posts/my.html",
            context={"flash": flash, "user": user, "posts": posts},
        )

    @get("/posts/{post_id:uuid}/edit")
    async def edit_post_page(
        self, request: "Request", db_session: AsyncSession, post_id: UUID
    ) -> TemplateResponse:
        """Show edit post form."""
        user = await self._require_auth(request, db_session)

        # Check ownership
        if not await page_service.check_page_ownership(db_session, post_id, user.id):
            raise NotAuthorizedException("You don't have permission to edit this post")

        post = await page_service.get_page_by_id(db_session, post_id)
        if not post or post.type != PageType.POST:
            raise NotFoundException("Post not found")

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "posts/edit.html",
            context={"flash": flash, "user": user, "post": post},
        )

    @post("/posts/{post_id:uuid}/edit")
    async def update_post(
        self, request: "Request", db_session: AsyncSession, post_id: UUID, data: FormMultiDict
    ) -> Redirect:
        """Update an existing post."""
        user = await self._require_auth(request, db_session)

        # Check ownership
        if not await page_service.check_page_ownership(db_session, post_id, user.id):
            raise NotAuthorizedException("You don't have permission to edit this post")

        title = data.get("title", "").strip()
        slug = data.get("slug", "").strip()
        content = data.get("content", "").strip()
        is_published = data.get("is_published") == "on"

        if not title or not slug:
            request.session["flash"] = "Title and slug are required"
            return Redirect(path=f"/posts/{post_id}/edit")

        # Get current post to check if we're publishing for the first time
        current_post = await page_service.get_page_by_id(db_session, post_id)
        published_at = current_post.published_at

        # Set published_at if publishing for the first time
        if is_published and not current_post.is_published:
            published_at = datetime.now(UTC)

        try:
            await page_service.update_page(
                db_session,
                page_id=post_id,
                slug=slug,
                title=title,
                content=content,
                is_published=is_published,
                published_at=published_at,
            )
            request.session["flash"] = f"Post '{title}' updated successfully!"
            return Redirect(path="/posts/my")
        except Exception as e:
            request.session["flash"] = f"Error updating post: {str(e)}"
            return Redirect(path=f"/posts/{post_id}/edit")

    @post("/posts/{post_id:uuid}/delete")
    async def delete_post(
        self, request: "Request", db_session: AsyncSession, post_id: UUID
    ) -> Redirect:
        """Delete a post."""
        user = await self._require_auth(request, db_session)

        # Check ownership
        if not await page_service.check_page_ownership(db_session, post_id, user.id):
            raise NotAuthorizedException("You don't have permission to delete this post")

        post = await page_service.get_page_by_id(db_session, post_id)
        post_title = post.title if post else "Unknown"

        if await page_service.delete_page(db_session, post_id):
            request.session["flash"] = f"Post '{post_title}' deleted successfully!"
        else:
            request.session["flash"] = "Failed to delete post"

        return Redirect(path="/posts/my")
