import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import bpy

from .RegistrationUtils import safe_register_class, safe_unregister_class


GITHUB_OWNER = "knickita"
GITHUB_REPOSITORY = "stagehand"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
USER_AGENT = "Stagehand-Blender-Addon-Updater"
PROTECTED_TOP_LEVEL_NAMES = {".git", ".github"}
IGNORED_NAMES = {"__pycache__"}


def _addon_directory():
    return Path(__file__).resolve().parent


def _manifest_path():
    return _addon_directory() / "blender_manifest.toml"


def _read_manifest_version():
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        return "0.0.0"

    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("version"):
            continue
        _, _, value = stripped.partition("=")
        return value.strip().strip("\"'")

    return "0.0.0"


def _version_tuple(version):
    cleaned = version.strip().lower()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]

    parts = []
    for chunk in cleaned.replace("-", ".").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if digits:
            parts.append(int(digits))

    return tuple(parts)


def _request_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_file(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _asset_score(asset):
    name = asset.get("name", "").lower()
    if not name.endswith(".zip"):
        return -1
    if "stagehand" in name:
        return 2
    return 1


def _release_download_url(release):
    assets = sorted(release.get("assets", ()), key=_asset_score, reverse=True)
    for asset in assets:
        if _asset_score(asset) > 0 and asset.get("browser_download_url"):
            return asset["browser_download_url"]

    zipball_url = release.get("zipball_url")
    if zipball_url:
        return zipball_url

    raise RuntimeError("Latest GitHub release does not contain a downloadable zip")


def _is_addon_root(path):
    return (path / "__init__.py").is_file() and (
        (path / "blender_manifest.toml").is_file()
        or (path / "Catalogue.json").is_file()
    )


def _find_extracted_addon_root(extract_directory):
    extract_directory = Path(extract_directory)
    if _is_addon_root(extract_directory):
        return extract_directory

    direct_candidates = [item for item in extract_directory.iterdir() if item.is_dir()]
    for candidate in direct_candidates:
        if _is_addon_root(candidate):
            return candidate

    for init_path in extract_directory.rglob("__init__.py"):
        candidate = init_path.parent
        if _is_addon_root(candidate):
            return candidate

    raise RuntimeError("Downloaded zip does not contain a valid Stagehand addon")


def _safe_extract_zip(archive, destination):
    destination = Path(destination)
    resolved_destination = destination.resolve()

    for member in archive.infolist():
        member_path = destination / member.filename
        resolved_member_path = member_path.resolve()
        if resolved_member_path != resolved_destination and resolved_destination not in resolved_member_path.parents:
            raise RuntimeError(f"Refusing to extract unsafe zip member: {member.filename}")

    archive.extractall(destination)


def _ensure_inside_directory(path, parent):
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    if resolved_path == resolved_parent or resolved_parent in resolved_path.parents:
        return
    raise RuntimeError(f"Refusing to write outside addon directory: {resolved_path}")


def _remove_path(path, addon_directory):
    _ensure_inside_directory(path, addon_directory)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _sync_directory(source, target, addon_directory):
    source = Path(source)
    target = Path(target)

    for target_child in target.iterdir():
        if target_child.name in IGNORED_NAMES:
            continue
        if target == addon_directory and target_child.name in PROTECTED_TOP_LEVEL_NAMES:
            continue

        source_child = source / target_child.name
        if not source_child.exists():
            _remove_path(target_child, addon_directory)

    for source_child in source.iterdir():
        if source_child.name in IGNORED_NAMES:
            continue
        if target == addon_directory and source_child.name in PROTECTED_TOP_LEVEL_NAMES:
            continue

        target_child = target / source_child.name
        _ensure_inside_directory(target_child, addon_directory)

        if source_child.is_dir() and not source_child.is_symlink():
            if target_child.exists() and not target_child.is_dir():
                _remove_path(target_child, addon_directory)
            target_child.mkdir(exist_ok=True)
            _sync_directory(source_child, target_child, addon_directory)
        else:
            if target_child.exists() and target_child.is_dir():
                _remove_path(target_child, addon_directory)
            shutil.copy2(source_child, target_child)


def _reload_scripts_timer():
    try:
        bpy.ops.script.reload()
    except Exception as exc:
        print(f"Stagehand addon updated, but automatic reload failed: {exc}")
    return None


def _schedule_script_reload():
    if bpy.app.timers.is_registered(_reload_scripts_timer):
        bpy.app.timers.unregister(_reload_scripts_timer)
    bpy.app.timers.register(_reload_scripts_timer, first_interval=0.5)


def current_version():
    return _read_manifest_version()


def install_latest_release():
    current = current_version()
    release = _request_json(GITHUB_API_URL)
    latest = release.get("tag_name") or release.get("name") or "latest"

    current_tuple = _version_tuple(current)
    latest_tuple = _version_tuple(latest)
    if current_tuple and latest_tuple and latest_tuple <= current_tuple:
        return False, f"Stagehand is already up to date ({current})"

    addon_directory = _addon_directory()
    download_url = _release_download_url(release)

    with tempfile.TemporaryDirectory(prefix="stagehand_update_") as temp_name:
        temp_directory = Path(temp_name)
        archive_path = temp_directory / "stagehand_release.zip"
        extract_directory = temp_directory / "extracted"
        extract_directory.mkdir()

        _download_file(download_url, archive_path)
        with zipfile.ZipFile(archive_path, "r") as archive:
            _safe_extract_zip(archive, extract_directory)

        extracted_addon = _find_extracted_addon_root(extract_directory)
        _sync_directory(extracted_addon, addon_directory, addon_directory)

    _schedule_script_reload()
    return True, f"Stagehand updated from {current} to {latest}. Reloading addon scripts"


class STAGEHAND_OT_update_addon(bpy.types.Operator):
    bl_idname = "stagehand.update_addon"
    bl_label = "Update Stagehand Addon"
    bl_description = "Download and install the latest Stagehand GitHub Release"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, _context):
        try:
            _updated, message = install_latest_release()
        except urllib.error.URLError as exc:
            self.report({'ERROR'}, f"Unable to reach GitHub: {exc}")
            return {'CANCELLED'}
        except PermissionError as exc:
            self.report({'ERROR'}, f"Unable to update addon files: {exc}")
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_update_addon,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
