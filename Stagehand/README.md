# Stagehand

## Power Lines

Use **Generate Power Lines** from the Stagehand panel to calculate cable routes and create one generated mesh named `Stagehand Power Lines`.

Power line generation uses the truss/pipe cable anchor graph as the routing graph. Fixtures are collected from visible Stagehand objects with power input links. Every real generated powerline must be assigned to one visible 16A single-phase output link, `POWER_OUT_CEE16A_MONO` / link type `4`.

If the scene does not contain enough 16A outputs, generation stops with an error that reports how many powerlines are required, how many 16A outputs are available, and how many outputs are missing.

Each powerline is routed from its assigned 16A output to the fixture power inputs it feeds. Shared truss segments are rendered as one cable bundle containing the lines that pass through that segment.

### Cable Obstacles

Use **Add Cable Obstacle** to create a cube obstacle volume. Cable obstacles can be moved, scaled, and rotated. During powerline generation, anchor points inside cable obstacle volumes are removed from the routing graph, so cables route around those blocked areas when another path is available.

All cable obstacles are stored in the `cable obstacles` collection.

### Cable Draw Face

The **Cable Draw Face** setting controls mesh face generation:

- `Only Visible`: generate only the external visible cable faces.
- `All`: generate each cable as a full individual mesh, including internal faces.

Changing this setting regenerates the generated powerline mesh.

### Cable Color

The **Cable Color** setting controls generated cable coloring:

- `Black`: draw all generated cables black.
- `Color Powerlines`: assign a different vertex color to each powerline.

Changing this setting regenerates the generated powerline mesh.

### Anchor Points

Use **Show/Hide Anchor Points** to display generated cable anchor markers. The anchor point object is generated, unselectable, and refreshed when Stagehand structures or cable obstacles move.