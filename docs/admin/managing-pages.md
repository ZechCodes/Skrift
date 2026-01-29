# Managing Pages

Learn how to create and manage pages using the Skrift admin interface.

## Overview

The Pages admin provides a content management interface for creating and editing site pages. Each page has a URL slug, title, content, and publication status.

## Accessing the Page Manager

Navigate to `/admin/pages` in your browser. You must be logged in with the `manage-pages` permission (included in the **admin** and **editor** roles).

## Page List

The page list displays all pages with:

| Column | Description |
|--------|-------------|
| **Title** | Page title (links to the public page) |
| **Slug** | URL path for the page |
| **Status** | Published or Draft |
| **Created** | Creation date |
| **Actions** | Edit, Publish/Unpublish, Delete |

## Creating a New Page

1. Click **New Page** from the page list
2. Fill in the form fields:

| Field | Description | Required |
|-------|-------------|----------|
| **Title** | Display title for the page | Yes |
| **Slug** | URL path (e.g., `about` becomes `/about`) | Yes |
| **Content** | Page body content | No |
| **Published** | Whether the page is visible to the public | No |

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

Pages have two states:

| Status | Description |
|--------|-------------|
| **Draft** | Only visible to users with `view-drafts` permission |
| **Published** | Visible to all visitors |

### Publishing a Page

From the page list, click **Publish** next to any draft page. The page becomes immediately visible to the public.

### Unpublishing a Page

Click **Unpublish** next to any published page. The page reverts to draft status and is hidden from public visitors.

!!! note "Draft Visibility"
    Users with the **admin**, **editor**, **author**, or **moderator** roles can view draft pages via their direct URL.

## Deleting a Page

1. Click **Delete** next to the page
2. Confirm the deletion in the browser dialog

!!! warning "Permanent Action"
    Deletion is permanent. There is no undo or trash feature.

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

## Required Permission

All page management routes require the `manage-pages` permission. This permission is included in:

- **admin** role (has all permissions)
- **editor** role

## Next Steps

- [Custom Admin Pages](custom-admin-pages.md) - Extend the admin interface
- [Creating Pages](../guides/creating-pages.md) - Template-based page creation
- [Custom Controllers](../guides/custom-controllers.md) - Build custom functionality
