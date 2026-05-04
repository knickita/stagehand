import importlib
import sys


def _load_submodule(module_name):
    qualified_name = f"{__name__}.{module_name}"
    existing_module = sys.modules.get(qualified_name)
    if existing_module is not None:
        return importlib.reload(existing_module)
    return importlib.import_module(f".{module_name}", __name__)


solver = _load_submodule("solver")
scene = _load_submodule("scene")
mesh = _load_submodule("mesh")
operators = _load_submodule("operators")


def register():
    operators.register()


def unregister():
    operators.unregister()
