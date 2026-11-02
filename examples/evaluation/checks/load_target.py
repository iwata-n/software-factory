import importlib.util
from pathlib import Path


def target():
    specification = importlib.util.spec_from_file_location(
        "factory_evaluation_target", Path(__file__).parents[1] / "app.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
