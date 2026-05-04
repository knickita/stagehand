# stagehand
plugin to implement the old stagehand unity project inside blender
 
See the Unity implementation: [stagehand-unity on GitHub](https://github.com/knickita/stagehand-unity)

## Custom Commands for Stagehand objects

- `L`: Enter Stagehand link move mode for the selected Stagehand object(s); move with the mouse, snap to compatible free links based on the current view, click/Enter to confirm, or Esc/right-click to cancel.

- `TAB`: Enter link mode, so that you can click on a link and add a compatible object in place

- `SPACE`: you can write the name of the object you like to add

## PDF generation performance

PDF drawing export prefers Blender's Workbench render engine with Freestyle disabled. This keeps the white-fill technical drawing style while avoiding the high Freestyle render cost.

PDF drawing export also uses NumPy, when available in Blender's Python, to speed up image pixel conversion. If NumPy is not available, export falls back to a pure Python conversion loop and PDF generation can be significantly slower.

## Cable Settings

Use the `Stagehand` sidebar panel in the 3D Viewport to control generated cable display.

- `Draw Face`: choose `Only Visible` to generate only the external visible cable faces, or `All` to generate every cable face, including internal faces.

- `Cable Color`: choose `Black` to draw all generated cables in black, or `Color Powerlines` to give every powerline its own vertex color.

Changing either setting automatically regenerates the existing `Stagehand Power Lines` mesh.

## Cable Obstacle

Use `Stagehand > Add Cable Obstacle` to create a cube-shaped obstacle for generated power lines.

The object is a normal Blender cube, so it can be moved, rotated, and scaled freely. Cable obstacles are stored in a Blender collection named `cable obstacles`; when a new one is created, existing marked cable obstacles are moved into that collection too. During `Generate Power Lines`, any cable anchor points inside the obstacle volume are removed from the routing graph, forcing the solver to route around that space.

Cable obstacles are marked with the custom property `stagehand_power_obstacle = True`. Objects named `Cable Obstacle` are also treated as cable obstacles by the solver.
