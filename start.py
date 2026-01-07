## ensure that we can import all the files inside this folder from belnder, maybe we can remove it in the final plugin?
import sys
import os
script_dir = os.path.dirname(os.path.abspath(__file__))
script_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.append(script_dir)
script_dir = os.path.join(script_dir,"Stagehand")
if script_dir not in sys.path:
    sys.path.append(script_dir)
## END OF WORKAROUND ##

import sys
print("\n\n SCRIPT RELOADED!\n------------------------------")
moduleName="Stagehand"
if moduleName in sys.modules:
    del sys.modules[moduleName]

    dotted = moduleName + "."
    for name in tuple(sys.modules):
        if name.startswith(dotted):
            del sys.modules[name]

import Stagehand