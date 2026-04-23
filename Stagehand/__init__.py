bl_info = {
    "name": "Stagehand",
    "author": "Nick",
    "version": (0, 0, 1),
    "blender": (3, 0, 0)
}

# pylint: disable=fixme, import-error
from . import AddStagehandObject
from . import Connections
from . import FirstPersonLook
from . import LinkMode
from . import LoadCatalogue
from . import MenuConfiguration
from . import OptionsPanel
from . import PdfDrawings
from . import SnapLogics

classes = (    
    AddStagehandObject,
    Connections,
    FirstPersonLook,
    LinkMode,
    LoadCatalogue,
    PdfDrawings,
    MenuConfiguration,
    OptionsPanel,
    SnapLogics,
)


def register():
    for cls in classes:
        cls.register()


def unregister():
    for cls in reversed(classes):
        try:
            cls.unregister()
        except Exception as x:
            print(x)
            continue


if __name__ == "__main__":
    register()
