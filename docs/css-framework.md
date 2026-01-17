# CSS Framework Documentation

A custom, minimalist CSS framework with neutral grey theming, pill-style buttons, and automatic light/dark mode support.

## Design Principles

- Clean, minimalist aesthetic
- Generous whitespace
- Subtle shadows and transitions
- Pill-style buttons (large border-radius)
- Thin, minimal input borders
- System font stack for fast loading
- Automatic light/dark mode via system preference

## Quick Start

Include the stylesheet in your HTML:

```html
<link rel="stylesheet" href="/static/css/style.css">
```

The framework automatically detects your system's color scheme preference. No JavaScript required.

## Color Scheme

### Automatic Detection

The framework uses `prefers-color-scheme` media query to automatically apply the appropriate theme based on system settings.

### Manual Override

Force a specific theme by adding `data-theme` to the `<html>` element:

```html
<html lang="en" data-theme="light">
<html lang="en" data-theme="dark">
```

### Color Palette

| Variable | Light Mode | Dark Mode | Usage |
|----------|------------|-----------|-------|
| `--color-bg` | `#fafafa` | `#121212` | Page background |
| `--color-surface` | `#f0f0f0` | `#1e1e1e` | Cards, elevated surfaces |
| `--color-text` | `#1a1a1a` | `#e5e5e5` | Primary text |
| `--color-text-muted` | `#6b6b6b` | `#9a9a9a` | Secondary text |
| `--color-primary` | `#1a1a1a` | `#e5e5e5` | Buttons, links |
| `--color-primary-hover` | `#333333` | `#ffffff` | Button hover state |
| `--color-primary-text` | `#fafafa` | `#121212` | Text on primary buttons |
| `--color-border` | `#e0e0e0` | `#2e2e2e` | Borders, dividers |
| `--color-success` | `#10b981` | `#34d399` | Success states |
| `--color-error` | `#ef4444` | `#f87171` | Error states |

## Layout

### Container

Use `.container` for centered, max-width content:

```html
<main class="container">
    <!-- Content here -->
</main>
```

- Max-width: 720px
- Centered with auto margins
- Responsive padding

### Semantic Sections

```html
<header class="container">
    <nav>...</nav>
</header>

<main class="container">
    <section>...</section>
</main>

<footer class="container">
    <p>...</p>
</footer>
```

## Typography

### Headings

```html
<h1>Heading 1</h1>  <!-- 2.25rem -->
<h2>Heading 2</h2>  <!-- 1.875rem -->
<h3>Heading 3</h3>  <!-- 1.5rem -->
<h4>Heading 4</h4>  <!-- 1.25rem -->
<h5>Heading 5</h5>  <!-- 1.125rem -->
<h6>Heading 6</h6>  <!-- 1rem -->
```

### Heading Groups

Use `<hgroup>` for title + subtitle combinations:

```html
<hgroup>
    <h1>Welcome</h1>
    <p>A brief description or tagline</p>
</hgroup>
```

### Text Utilities

```html
<p class="text-center">Centered text</p>
<p class="text-muted">Muted/secondary text</p>
<small>Small text (automatically muted)</small>
```

### Code

```html
<code>inline code</code>

<pre>
code block
with multiple lines
</pre>
```

## Buttons

### Primary Button

Buttons use pill-style (fully rounded) by default:

```html
<button>Click Me</button>
<a href="/action" role="button">Link Button</a>
<input type="submit" value="Submit">
```

### Outline Button

For secondary actions:

```html
<button class="outline">Secondary Action</button>
<a href="/cancel" role="button" class="outline">Cancel</a>
```

### Button States

- **Hover**: Slightly lighter background, elevated shadow
- **Active**: Pressed down effect
- **Disabled**: Reduced opacity, no pointer

```html
<button disabled>Disabled</button>
<a role="button" aria-disabled="true">Disabled Link</a>
```

## Forms

### Text Inputs

```html
<label for="name">Name</label>
<input type="text" id="name" placeholder="Enter your name">

<label for="email">Email</label>
<input type="email" id="email">

<label for="message">Message</label>
<textarea id="message"></textarea>
```

### Select

```html
<label for="country">Country</label>
<select id="country">
    <option>United States</option>
    <option>Canada</option>
</select>
```

### Checkboxes and Radio Buttons

```html
<label>
    <input type="checkbox"> Remember me
</label>

<label>
    <input type="radio" name="plan"> Free
</label>
<label>
    <input type="radio" name="plan"> Pro
</label>
```

### Fieldset

```html
<fieldset>
    <legend>Account Details</legend>
    <label for="user">Username</label>
    <input type="text" id="user">
</fieldset>
```

## Cards / Articles

```html
<article>
    <header>
        <h2>Card Title</h2>
    </header>

    <p>Card content goes here.</p>

    <footer>
        <a href="/action" role="button">Action</a>
    </footer>
</article>
```

Note: Article headers and footers have no border dividers for a cleaner look.

## Alerts

Use `role="alert"` for notification messages:

```html
<article role="alert">
    This is an important message.
</article>
```

Alerts have a left accent border in the primary color.

### Flash Messages

```html
<div class="flash-messages">
    <article role="alert">Success! Your changes have been saved.</article>
</div>
```

## Navigation

```html
<nav>
    <ul>
        <li><strong><a href="/">Site Name</a></strong></li>
    </ul>
    <ul>
        <li><a href="/about">About</a></li>
        <li><a href="/login" role="button">Login</a></li>
    </ul>
</nav>
```

### User Info with Avatar

```html
<li class="user-info">
    <img src="/avatar.jpg" alt="User Name">
    <span>User Name</span>
</li>
```

## Tables

```html
<table>
    <thead>
        <tr>
            <th>Name</th>
            <th>Email</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>John Doe</td>
            <td>john@example.com</td>
        </tr>
    </tbody>
</table>
```

## Special Pages

### Error Pages

Add `.error-page` class to vertically center content:

```html
<main class="container error-page">
    <article>
        <header>
            <h1>404</h1>
        </header>
        <p>Page not found.</p>
        <footer>
            <a href="/" role="button">Return Home</a>
        </footer>
    </article>
</main>
```

In Jinja templates, use a block:

```jinja
{% block main_class %} error-page{% endblock %}
```

## CSS Variables Reference

### Spacing

| Variable | Value |
|----------|-------|
| `--spacing-xs` | 0.25rem |
| `--spacing-sm` | 0.5rem |
| `--spacing-md` | 1rem |
| `--spacing-lg` | 1.5rem |
| `--spacing-xl` | 2rem |
| `--spacing-2xl` | 3rem |

### Border Radius

| Variable | Value | Usage |
|----------|-------|-------|
| `--radius-sm` | 4px | Small elements |
| `--radius-md` | 8px | Inputs, small cards |
| `--radius-lg` | 12px | Cards, articles |
| `--radius-pill` | 999px | Buttons |

### Shadows

| Variable | Usage |
|----------|-------|
| `--shadow-sm` | Subtle elevation |
| `--shadow-md` | Hover states, elevated cards |

### Transitions

| Variable | Value |
|----------|-------|
| `--transition-fast` | 150ms ease |
| `--transition-normal` | 200ms ease |

### Typography

| Variable | Value |
|----------|-------|
| `--font-family` | System font stack |
| `--font-family-mono` | Monospace font stack |
| `--line-height` | 1.6 |
| `--line-height-tight` | 1.3 |

## Browser Support

- Modern browsers (Chrome, Firefox, Safari, Edge)
- Uses CSS custom properties (variables)
- Uses `prefers-color-scheme` media query
- No JavaScript required for theming
