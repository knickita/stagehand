bl_info = {
    "name": "Stagehand",
    "author": "Nick",
    "version": (0, 0, 1),
    "blender": (3, 0, 0)
}

import importlib
import sys

# pylint: disable=fixme, import-error


def _load_submodule(module_name):
    qualified_name = f"{__name__}.{module_name}"
    existing_module = sys.modules.get(qualified_name)
    if existing_module is not None:
        return importlib.reload(existing_module)
    return importlib.import_module(f".{module_name}", __name__)


AddStagehandObject = _load_submodule("AddStagehandObject")
ProjectDatabase = _load_submodule("ProjectDatabase")
Connections = _load_submodule("Connections")
FirstPersonLook = _load_submodule("FirstPersonLook")
LinkMode = _load_submodule("LinkMode")
LoadCatalogue = _load_submodule("LoadCatalogue")
MenuConfiguration = _load_submodule("MenuConfiguration")
MvrImport = _load_submodule("MvrImport")
Alerts = _load_submodule("Alerts")
OptionsPanel = _load_submodule("OptionsPanel")
PdfDrawings = _load_submodule("PdfDrawings")
PowerManagement = _load_submodule("PowerManagement")
SnapLogics = _load_submodule("SnapLogics")

classes = (    
    AddStagehandObject,
    ProjectDatabase,
    Connections,
    FirstPersonLook,
    LinkMode,
    LoadCatalogue,
    MvrImport,
    PdfDrawings,
    PowerManagement,
    MenuConfiguration,
    Alerts,
    OptionsPanel,
    SnapLogics,
)


def register():
    registered_modules = []
    try:
        for cls in classes:
            cls.register()
            registered_modules.append(cls)
    except Exception:
        for cls in reversed(registered_modules):
            try:
                cls.unregister()
            except Exception:
                continue
        raise


def unregister():
    for cls in reversed(classes):
        try:
            cls.unregister()
        except Exception as x:
            print(x)
            continue


if __name__ == "__main__":
    register()
