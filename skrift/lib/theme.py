"""Theme discovery and metadata for Skrift CMS.

Themes are optional directories under ./themes/ in the working directory.
Each theme must have a templates/ subdirectory and may optionally include
static/, theme.yaml metadata, and a screenshot.png preview.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ThemeInfo:
    """Metadata about a discovered theme."""

    directory_name: str
    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    templates_dir: Path = field(default_factory=Path)
    static_dir: Path | None = None
    screenshot: Path | None = None
    colors: list[str] = field(default_factory=list)


def get_themes_dir() -> Path:
    """Return the themes directory path (./themes/ relative to cwd)."""
    return Path(os.getcwd()) / "themes"


def themes_available() -> bool:
    """Check if a themes/ directory exists with at least one valid theme."""
    themes_dir = get_themes_dir()
    if not themes_dir.is_dir():
        return False

    for entry in themes_dir.iterdir():
        if entry.is_dir() and (entry / "templates").is_dir():
            return True

    return False


def discover_themes() -> list[ThemeInfo]:
    """Scan themes/ directory and return metadata for all valid themes."""
    themes_dir = get_themes_dir()
    if not themes_dir.is_dir():
        return []

    themes = []
    for entry in sorted(themes_dir.iterdir()):
        if not entry.is_dir():
            continue

        templates_dir = entry / "templates"
        if not templates_dir.is_dir():
            continue

        info = _parse_theme(entry)
        themes.append(info)

    return themes


def get_theme_info(name: str) -> ThemeInfo | None:
    """Look up a single theme by its directory name."""
    themes_dir = get_themes_dir()
    theme_dir = themes_dir / name
    if not theme_dir.is_dir() or not (theme_dir / "templates").is_dir():
        return None

    return _parse_theme(theme_dir)


def _parse_theme(theme_dir: Path) -> ThemeInfo:
    """Parse a theme directory into a ThemeInfo."""
    directory_name = theme_dir.name
    templates_dir = theme_dir / "templates"

    static_dir = theme_dir / "static"
    if not static_dir.is_dir():
        static_dir = None

    screenshot = theme_dir / "screenshot.png"
    if not screenshot.is_file():
        screenshot = None

    # Parse optional theme.yaml
    name = directory_name
    description = ""
    version = ""
    author = ""
    colors: list[str] = []

    metadata_file = theme_dir / "theme.yaml"
    if metadata_file.is_file():
        try:
            with open(metadata_file, "r") as f:
                meta = yaml.safe_load(f) or {}
            name = meta.get("name", directory_name)
            description = meta.get("description", "")
            version = meta.get("version", "")
            author = meta.get("author", "")
            raw_colors = meta.get("colors", [])
            if isinstance(raw_colors, list):
                colors = [str(c) for c in raw_colors]
        except Exception:
            pass

    return ThemeInfo(
        directory_name=directory_name,
        name=name,
        description=description,
        version=version,
        author=author,
        templates_dir=templates_dir,
        static_dir=static_dir,
        screenshot=screenshot,
        colors=colors,
    )
