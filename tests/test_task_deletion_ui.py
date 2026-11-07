"""Controller tests use Node and a DOM stand-in, not browser automation."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

UI = Path("src/software_factory/operations_ui")


def test_deletion_controller_interactions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the deletion controller unit tests")
    result = subprocess.run(
        [node, "--test", "tests/ui/task_deletion.test.mjs"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_deletion_dialog_accessibility_and_mobile_layout():
    class Nodes(HTMLParser):
        def __init__(self):
            super().__init__()
            self.nodes = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.nodes[attrs["id"]] = tag, attrs

    html = (UI / "index.html").read_text()
    parser = Nodes()
    parser.feed(html)
    tag, dialog = parser.nodes["delete-task-dialog"]
    assert tag == "dialog"
    assert dialog["aria-labelledby"] == "delete-task-heading"
    assert set(dialog["aria-describedby"].split()) == {
        "delete-task-description",
        "delete-task-warning",
    }
    assert "削除は取り消せません" in html
    assert "autofocus" in parser.nodes["delete-task-cancel"][1]
    assert parser.nodes["delete-task-confirm"][1]["type"] == "button"
    # Source-level layout checks; actual mobile rendering remains a browser check.
    css = (UI / "board.css").read_text()
    assert "max-width: min(30rem, calc(100vw - 28px))" in css
    assert "flex-wrap: wrap" in css


def test_deletion_module_is_served_with_board(world):
    asset = world.client.get("/operations/task_deletion.js?v=task-delete")
    assert asset.status_code == 200
    assert "export function installTaskDeletion" in asset.text
    assert "./task_deletion.js?v=task-delete" in world.client.get("/operations/board.js").text
