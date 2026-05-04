"""Tests for the ``skrift db`` CLI helpers."""

import argparse
import os
from pathlib import Path

from skrift.cli import _run_alembic


def test_run_alembic_uses_os_separator_for_paths_with_spaces(tmp_path, monkeypatch):
    project_root = tmp_path / "project with spaces"
    user_versions = project_root / "migrations" / "versions"
    user_versions.mkdir(parents=True)

    captured = {}

    def run_command(cfg):
        captured["cfg"] = cfg

    class Parser:
        def parse_args(self, args):
            captured["args"] = args
            return argparse.Namespace(cmd=(run_command, [], []))

        def error(self, message):
            raise AssertionError(message)

    class FakeCommandLine:
        def __init__(self):
            self.parser = Parser()

    monkeypatch.setattr("alembic.config.CommandLine", FakeCommandLine)

    _run_alembic(project_root, ["heads"])

    skrift_versions = str(
        Path(__file__).resolve().parent.parent / "skrift" / "alembic" / "versions"
    )
    expected_locations = [str(user_versions), skrift_versions]
    cfg = captured["cfg"]

    assert captured["args"] == ["heads"]
    assert cfg.get_main_option("path_separator") == "os"
    assert cfg.get_main_option("version_path_separator") == "os"
    assert cfg.get_main_option("version_locations") == os.pathsep.join(expected_locations)
    assert cfg.get_version_locations_list() == expected_locations
