"""Tests for theme metadata parsing."""

from unittest.mock import patch


def test_get_theme_info_logs_and_falls_back_on_invalid_metadata(tmp_path):
    from skrift.lib.theme import get_theme_info

    theme_dir = tmp_path / "themes" / "bad-theme"
    (theme_dir / "templates").mkdir(parents=True)
    (theme_dir / "theme.yaml").write_text("name: [invalid", encoding="utf-8")

    with patch("skrift.lib.theme.get_themes_dir", return_value=tmp_path / "themes"), \
         patch("skrift.lib.theme.logger.debug") as mock_log:
        info = get_theme_info("bad-theme")

    assert info is not None
    assert info.name == "bad-theme"
    mock_log.assert_called_once()
