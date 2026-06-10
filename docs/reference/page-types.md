# Page Types

A page type is a content kind ("post", "recipe", "project") declared in `app.yaml`. One entry generates the full stack for that type — admin CRUD, public routes, permissions, and template conventions. **Do not hand-write CRUD controllers for a declared page type; everything below already exists.**

## Declaring a Page Type

```yaml
page_types:
  - name: post          # singular, used in routes and templates
    plural: posts       # used in admin paths and permissions
    icon: pen-tool      # Lucide icon for the admin sidebar
    nav_order: 10       # admin sidebar sort (lower = higher)
  - name: page
    plural: pages
```

When `page_types` is omitted entirely, the default `page`/`pages` type is registered. Optional per-type key `subdomain: blog` serves the type on that subdomain instead of the primary domain (see [Multisite](../guides/multisite.md)).

## What One Entry Generates

For `name: post, plural: posts`:

### Admin section — `/admin/posts`

A generated controller (`skrift/admin/page_type_factory.py`) provides list, create, edit, publish, unpublish, delete, and revision-restore routes, all filtered to `Page.type == "post"`. It appears in the admin sidebar automatically with the configured `icon` and `nav_order`.

### Public routes

| Route | Behavior |
|-------|----------|
| `GET /post/` | Archive: published posts, newest first |
| `GET /post/{slug}` | Single post (drafts visible to logged-in users) |

With `subdomain: blog` set, the same routes serve at the subdomain root: `blog.example.com/` and `blog.example.com/{slug}`.

Single-page routes also support content negotiation: `Accept: text/markdown` returns the raw Markdown source.

### Permissions

Four permissions are derived from `plural` and registered at startup (`permissions_for_type` in `skrift/auth/roles.py`):

| Permission | Grants |
|------------|--------|
| `manage-posts` | Full control over all posts (added to the `admin` role) |
| `create-posts` | Create new posts (added to the `author` role) |
| `edit-own-posts` | Edit posts the user authored (added to `author`) |
| `delete-own-posts` | Delete posts the user authored (added to `author`) |

Use them like any other permission: `guards=[auth_guard, Permission("manage-posts")]`.

### Templates

Public rendering uses the [WordPress-style fallback resolver](../guides/custom-templates.md) (`skrift.template.Template`). Most specific name wins, searched theme dir → project `./templates/` → package defaults:

| Route | Tried in order |
|-------|----------------|
| `/post/{slug}` | `post-{slug}.html` → `post.html` |
| `/post/` (archive) | `archive-post.html` → `archive.html` |
| Subdomain root archive | `index.html` |

Single-page templates receive `page`, `seo_meta`, `og_meta`, `featured_image_url`, and `asset_urls` in context; archives receive `pages`. Both receive `page_type_name` and `page_type_plural`.

## Customizing Generated Behavior

Two filter hooks let extensions adjust admin behavior per page (used by the republish system to lock externally-managed pages):

```python
from skrift.hooks import PAGE_ADMIN_CAN_MUTATE, PAGE_ADMIN_PAGE_STATE, add_filter

# Return False to block edit/publish/unpublish/delete/restore for a page
add_filter(PAGE_ADMIN_CAN_MUTATE, my_can_mutate)   # (allowed, request, db_session, page, action)

# Add badges or a locked flag to the admin list view
add_filter(PAGE_ADMIN_PAGE_STATE, my_page_state)   # (state, request, db_session, page)
```

For behavior beyond what hooks cover, write a regular Litestar controller alongside the generated ones — the factories are public API (`skrift.admin.page_type_factory.create_page_type_controller`, `skrift.controllers.page_type_factory.create_public_page_type_controller`) if you want to build on them directly.
