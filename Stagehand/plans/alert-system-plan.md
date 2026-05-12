# Stagehand Alert System Plan

Working document for the implemented alert system.

## Current Architecture

- Alerts are transient runtime state, not saved in the `.blend` file.
- `Alerts.py` owns scanning, alert records, UI helper, operators, and worker lifecycle.
- The Stagehand sidebar calls `Alerts.draw_alerts(layout, context)` from `OptionsPanel.py`.
- `Alerts` is registered before `OptionsPanel` in `__init__.py`.
- There is no alert request queue populated by Stagehand operations.
- Stagehand operations do not notify the alert system directly.
- A persistent background worker thread scans the scene.
- A Blender main-thread timer publishes worker results into the visible alert store.

## Scanning

- Manual scan:
  - Button label: `Scan Now`
  - Operator: `stagehand.scan_alerts_now`
  - Runs a scan as soon as the background worker wakes.
- Automatic scan:
  - Checkbox label: `Auto Scan`
  - WindowManager property: `stagehand_alert_auto_scan`
  - When enabled, the background worker scans periodically.
  - Current interval: `Alerts.AUTO_SCAN_INTERVAL = 3.0` seconds.
- The sidebar always shows:
  - error count
  - warning count
  - `Auto Scan` checkbox
  - `Scan Now` button
  - running state
  - last scan timestamp
  - last scan result or last scan error

## Rules

### Nearby Unconnected Compatible Links

- Rule id: `nearby_unconnected_compatible_links`
- Severity: `warning`
- Scans all Stagehand objects.
- Considers only currently unconnected links.
- Requires compatible link types via `LinkTypes.are_link_types_compatible`.
- Requires exact current auto-connect alignment via `Connections.links_are_aligned`.
- Alert id is based on the two sorted link UIDs.

### Unconnected Power Input

- Rule id: `unconnected_power_input`
- Severity: `error`
- Scans all Stagehand objects.
- Uses `LinkTypes.is_power_input`.
- If a power input link is not connected, creates one alert for that link.
- Alert id is based on the rule id and link UID.

## UI Behavior

- Alerts appear inline in the existing Stagehand sidebar.
- The list is expanded/collapsed with a dedicated triangle button.
- Each alert row is an operator button carrying `alert_id`.
- Clicking an alert:
  - resolves objects by UID
  - selects involved objects
  - makes the first ordered object active
  - focuses the active 3D viewport on the alert center
- The Blender scene camera is not moved.
- No viewport overlay or generated scene object is created for alerts.

## Threading Boundary

- The worker scans in the background.
- The worker may read Blender data directly by current design.
- The worker must not mutate Blender data, UI state, selection, or view state.
- Worker results go through a thread-safe result queue.
- The Blender timer drains the result queue and updates visible alert state on the main thread.

## Residual Risk

- Blender Python API access from background threads is still the main technical risk.
- If instability appears, move scene reading into a main-thread snapshot step while keeping the same UI/rule/result model.
