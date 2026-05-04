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
