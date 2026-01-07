imports=[
    "FirstPersonLook",
    "LoadCatalogue"
]

#reload a class registered in blender, useful during development, to update the changes in code. maybe we can remove it in the final version
def Reload(moduleName):
    cls=__import__(moduleName)
    try:
        cls.unregister()
    except:
        pass
    cls.register()

import sys
for moduleName in imports:
    if moduleName in sys.modules:
        del sys.modules[moduleName]    
    Reload(moduleName)
