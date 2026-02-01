# Markdown Content

<span class="skill-badge beginner">:material-star: Beginner</span>

Write page content using Markdown syntax instead of raw HTML.

## Overview

Page content supports Markdown formatting, which is automatically converted to HTML when displayed. This makes it easier to write and maintain content without dealing with HTML tags directly.

## Basic Formatting

### Headings

```markdown
# Heading 1
## Heading 2
### Heading 3
#### Heading 4
```

### Text Styles

```markdown
**Bold text**
*Italic text*
***Bold and italic***
`Inline code`
```

### Lists

**Unordered list:**
```markdown
- Item one
- Item two
- Item three
```

**Ordered list:**
```markdown
1. First item
2. Second item
3. Third item
```

### Blockquotes

```markdown
> This is a blockquote.
> It can span multiple lines.
```

## Links

```markdown
[Link text](https://example.com)
[Link with title](https://example.com "Title text")
```

## Images

```markdown
![Alt text](/static/images/photo.png)
![Image with title](/static/images/photo.png "Image title")
```

## Code Blocks

**Inline code:**
```markdown
Use `code` for inline code.
```

**Fenced code blocks:**
````markdown
```
code block
```
````

**With language syntax highlighting:**
````markdown
```python
def hello():
    print("Hello, world!")
```
````

## Tables

```markdown
| Column 1 | Column 2 | Column 3 |
|----------|----------|----------|
| Cell 1   | Cell 2   | Cell 3   |
| Cell 4   | Cell 5   | Cell 6   |
```

### Column Alignment

```markdown
| Left     | Center   | Right    |
|:---------|:--------:|---------:|
| Left     | Center   | Right    |
```

- `:---` aligns left
- `:---:` centers
- `---:` aligns right

## Footnotes

Add footnotes to provide additional context:

```markdown
Here is some text with a footnote[^1].

Another statement with a footnote[^note].

[^1]: This is the first footnote.
[^note]: Footnotes can have any identifier.
```

Footnotes are collected and displayed at the bottom of the rendered content.

## Example Page Content

```markdown
# Welcome to Our Site

We're glad you're here! This page demonstrates **Markdown** formatting.

## Our Services

We offer the following:

- Web Development
- Design Services
- Consulting

## Contact Us

| Method | Details |
|--------|---------|
| Email  | hello@example.com |
| Phone  | 555-1234 |

For more information, visit our [contact page](/contact).

---

*Last updated: January 2026*
```

## Technical Details

Skrift uses [markdown-it-py](https://github.com/executablebooks/markdown-it-py) with CommonMark compliance for rendering. The following features are enabled:

- **CommonMark** - Standard Markdown specification
- **Tables** - GitHub-flavored Markdown tables
- **Footnotes** - Reference-style footnotes
- **Typographer** - Smart quotes and typography improvements

## Next Steps

- [Creating Pages](creating-pages.md) - Learn how to create and manage pages
- [CSS Framework](../reference/css-framework.md) - Style your rendered content
