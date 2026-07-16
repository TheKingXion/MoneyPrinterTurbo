import ast
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _load_should_open_task_folder():
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_should_open_task_folder"
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace["_should_open_task_folder"]


should_open_task_folder = _load_should_open_task_folder()


def test_task_folder_only_opens_for_local_desktop_server():
    assert should_open_task_folder("127.0.0.1", running_in_container=False)
    assert should_open_task_folder("localhost", running_in_container=False)
    assert should_open_task_folder("::1", running_in_container=False)


def test_task_folder_does_not_open_for_remote_or_container_server():
    assert not should_open_task_folder("0.0.0.0", running_in_container=False)
    assert not should_open_task_folder("192.168.1.20", running_in_container=False)
    assert not should_open_task_folder("127.0.0.1", running_in_container=True)
