bl_info = {
    "name": "Stagehand",
    "author": "Nick",
    "version": (0, 0, 1),
    "blender": (3, 0, 0)
}

# pylint: disable=fixme, import-error
from . import AddStagehandObject
from . import FirstPersonLook
from . import LinkMode
from . import LoadCatalogue
from . import MenuConfiguration
from . import OptionsPanel

classes = (    
    AddStagehandObject,
    FirstPersonLook,
    LinkMode,
    LoadCatalogue,
    MenuConfiguration,
    OptionsPanel,
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
