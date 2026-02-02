# Managing Pages

Learn how to create and manage pages using the Skrift admin interface.

## Overview

The Pages admin provides a content management interface for creating and editing site pages. Each page has a URL slug, title, content, publication status, and optional SEO metadata.

## Accessing the Page Manager

Navigate to `/admin/pages` in your browser. You must be logged in with the `manage-pages` permission (included in the **admin** and **editor** roles).

## Page List

The page list displays all pages with:

| Column | Description |
|--------|-------------|
| **Order** | Display order (lower numbers first) |
| **Title** | Page title (links to the public page) |
| **Slug** | URL path for the page |
| **Status** | Published, Draft, or Scheduled |
| **Created** | Creation date |
| **Actions** | Edit, Publish/Unpublish, Delete |

## Creating a New Page

1. Click **New Page** from the page list
2. Fill in the form fields:

| Field | Description | Required |
|-------|-------------|----------|
| **Title** | Display title for the page | Yes |
| **Slug** | URL path (e.g., `about` becomes `/about`) | Yes |
| **Content** | Page body content (supports Markdown) | No |
| **Published** | Whether the page is visible to the public | No |
| **Display Order** | Sort order in lists (lower numbers first) | No |
| **Schedule Publish** | Future date/time to make page visible | No |

3. Click **Create Page**

### Slug Format

The slug determines the page URL:

- `about` → `/about`
- `services/consulting` → `/services/consulting`
- `team/leadership` → `/team/leadership`

!!! tip "Slug Guidelines"
    - Use lowercase letters
    - Separate words with hyphens: `about-us`
    - Avoid special characters
    - Keep slugs short and descriptive

## Editing a Page

1. Click **Edit** next to the page in the list
2. Modify any fields
3. Click **Update Page**

Changes are saved immediately. If the page is published, visitors will see the updates right away.

## Publication Status

Pages have three states:

| Status | Description |
|--------|-------------|
| **Draft** | Only visible to users with `view-drafts` permission |
| **Published** | Visible to all visitors |
| **Scheduled** | Published but waiting for scheduled date |

### Publishing a Page

From the page list, click **Publish** next to any draft page. The page becomes immediately visible to the public.

### Unpublishing a Page

Click **Unpublish** next to any published page. The page reverts to draft status and is hidden from public visitors.

!!! note "Draft Visibility"
    Users with the **admin**, **editor**, **author**, or **moderator** roles can view draft pages via their direct URL.

## Content Scheduling

You can schedule pages to publish at a future date:

1. Edit the page (or create a new one)
2. Check the **Published** checkbox
3. Set the **Schedule Publish** date and time
4. Save the page

The page will show as "Scheduled" in the admin list until the scheduled time, at which point it becomes visible to the public.

!!! tip "Scheduling Tips"
    - The page must have **Published** checked for scheduling to work
    - Leave the schedule field empty to publish immediately
    - Scheduled pages won't appear in sitemaps until their publish date

## Page Ordering

Control the display order of pages:

1. Set the **Display Order** field when creating or editing a page
2. Lower numbers appear first (0 is the default)
3. Pages with the same order are sorted by creation date

This is useful for controlling navigation menus or page lists in templates.

## SEO Settings

Each page has optional SEO metadata accessible via the collapsible **SEO Settings** section:

| Field | Description |
|-------|-------------|
| **Meta Description** | Description for search engines (max 320 chars) |
| **Meta Robots** | Indexing directive (noindex, nofollow, etc.) |
| **OpenGraph Title** | Custom title for social sharing |
| **OpenGraph Description** | Custom description for social sharing |
| **OpenGraph Image URL** | Image URL for social sharing previews |

These settings are automatically rendered in the page's `<head>` section.

!!! tip "SEO Best Practices"
    - Keep meta descriptions under 160 characters for best display
    - Use OpenGraph fields to control how links appear on social media
    - Use `noindex` for pages you don't want in search results

## Page Revisions

Skrift automatically tracks content history when you edit pages:

### Viewing Revision History

1. Edit a page
2. Click **View History** at the bottom of the form
3. See all previous versions with timestamps and authors

### Restoring a Previous Version

1. From the revision history, click **Restore** next to any revision
2. Confirm the action
3. The page content is restored to that version

!!! note "Revision Safety"
    When you restore a revision, Skrift first saves the current content as a new revision. This means you can always undo a restore.

## Deleting a Page

1. Click **Delete** next to the page
2. Confirm the deletion in the browser dialog

!!! warning "Permanent Action"
    Deletion is permanent and includes all revision history. There is no undo or trash feature.

## Admin Routes Reference

| Route | Method | Action |
|-------|--------|--------|
| `/admin/pages` | GET | List all pages |
| `/admin/pages/new` | GET | Show new page form |
| `/admin/pages/new` | POST | Create new page |
| `/admin/pages/{id}/edit` | GET | Show edit form |
| `/admin/pages/{id}/edit` | POST | Update page |
| `/admin/pages/{id}/publish` | POST | Publish page |
| `/admin/pages/{id}/unpublish` | POST | Unpublish page |
| `/admin/pages/{id}/delete` | POST | Delete page |
| `/admin/pages/{id}/revisions` | GET | View revision history |
| `/admin/pages/{id}/revisions/{rev_id}/restore` | POST | Restore revision |

## Required Permission

All page management routes require the `manage-pages` permission. This permission is included in:

- **admin** role (has all permissions)
- **editor** role

## Next Steps

- [Custom Admin Pages](custom-admin-pages.md) - Extend the admin interface
- [Creating Pages](../guides/creating-pages.md) - Template-based page creation
- [Custom Controllers](../guides/custom-controllers.md) - Build custom functionality
