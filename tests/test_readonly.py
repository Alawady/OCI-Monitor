"""Guard: the package must never call a mutating OCI API."""

import ast
from pathlib import Path

import ocimon

MUTATING = ("create_", "update_", "delete_", "terminate_", "instance_action", "start_", "stop_", "reset_",
            "change_", "attach_", "detach_", "launch_", "put_", "patch_", "restore_", "move_", "cancel_",
            "rename_", "reboot", "scale_", "enable_", "disable_", "upload_", "copy_", "install_")


def test_no_mutating_sdk_calls():
    root = Path(ocimon.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                name = node.value  # getattr(client, "list_vcns") style
            if name and name.startswith(MUTATING):
                offenders.append(f"{path.relative_to(root)}:{node.lineno} {name}")
    assert offenders == []
