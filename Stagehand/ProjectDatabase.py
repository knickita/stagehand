import bpy


DATABASE_OBJECT_NAME = "database"
DATABASE_MARKER_KEY = "stagehand_is_database"
DATABASE_VERSION_KEY = "stagehand_database_version"
CONNECTIONS_KEY = "stagehand_connections"
LINK_PARENTS_KEY = "stagehand_link_parents"
OBJECT_NAMES_KEY = "stagehand_object_names"
DATABASE_SELECT_GUARD_INTERVAL = 0.5
STAGEHAND_COLLECTION_NAME = "stagehand"


def _data_objects():
    return getattr(bpy.data, "objects", None)


def _data_scenes():
    return getattr(bpy.data, "scenes", None)


def _stagehand_collection():
    collection = bpy.data.collections.get(STAGEHAND_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(STAGEHAND_COLLECTION_NAME)

    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        if all(child != collection for child in scene.collection.children):
            scene.collection.children.link(collection)
        return collection

    scenes = _data_scenes()
    if scenes is not None:
        for candidate_scene in scenes:
            if all(child != collection for child in candidate_scene.collection.children):
                candidate_scene.collection.children.link(collection)
                break

    return collection


def _move_to_stagehand_collection(obj):
    collection = _stagehand_collection()
    if all(existing != obj for existing in collection.objects):
        collection.objects.link(obj)

    for user_collection in list(obj.users_collection):
        if user_collection != collection:
            user_collection.objects.unlink(obj)


def _coerce_string_dict(value):
    if value is None:
        return {}

    items = getattr(value, "items", None)
    if items is None:
        return {}

    return {str(key): str(item) for key, item in items()}


def _set_mapping_value(obj, key, mapping):
    normalized = {
        str(map_key): str(map_value)
        for map_key, map_value in dict(mapping).items()
        if str(map_key)
    }
    obj[key] = normalized


def _ensure_database_shape(obj):
    obj[DATABASE_MARKER_KEY] = True
    obj[DATABASE_VERSION_KEY] = 1
    obj.hide_select = True
    obj.hide_viewport = True
    obj.hide_render = True
    obj.empty_display_type = 'PLAIN_AXES'
    obj.empty_display_size = 0.25

    for key in (CONNECTIONS_KEY, LINK_PARENTS_KEY, OBJECT_NAMES_KEY):
        current_value = _coerce_string_dict(obj.get(key))
        if key not in obj or dict(current_value) != current_value:
            _set_mapping_value(obj, key, current_value)


def _lock_database_selection(obj):
    obj.hide_select = True
    obj.hide_viewport = True
    obj.hide_render = True
    obj.select_set(False)

    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None and view_layer.objects.active == obj:
        view_layer.objects.active = None


def _link_database_to_scene(obj):
    _move_to_stagehand_collection(obj)


def get_database_object(create=False):
    objects = _data_objects()
    if objects is None:
        return None

    database_object = objects.get(DATABASE_OBJECT_NAME)
    if database_object is not None and (
        database_object.get(DATABASE_MARKER_KEY)
        or getattr(database_object, "type", "") == 'EMPTY'
    ):
        if create:
            _ensure_database_shape(database_object)
        return database_object

    for candidate in objects:
        if candidate.get(DATABASE_MARKER_KEY):
            if create:
                _ensure_database_shape(candidate)
            return candidate

    if not create:
        return None

    database_object = bpy.data.objects.new(DATABASE_OBJECT_NAME, None)
    _ensure_database_shape(database_object)
    _link_database_to_scene(database_object)
    return database_object


def get_connections(create=False):
    database_object = get_database_object(create=create)
    if database_object is None:
        return {}
    return _coerce_string_dict(database_object.get(CONNECTIONS_KEY))


def set_connections(connections):
    database_object = get_database_object(create=True)
    if database_object is None:
        return
    _set_mapping_value(database_object, CONNECTIONS_KEY, connections)


def get_link_parents(create=False):
    database_object = get_database_object(create=create)
    if database_object is None:
        return {}
    return _coerce_string_dict(database_object.get(LINK_PARENTS_KEY))


def set_link_parents(link_parents):
    database_object = get_database_object(create=True)
    if database_object is None:
        return
    _set_mapping_value(database_object, LINK_PARENTS_KEY, link_parents)


def get_object_names(create=False):
    database_object = get_database_object(create=create)
    if database_object is None:
        return {}
    return _coerce_string_dict(database_object.get(OBJECT_NAMES_KEY))


def set_object_names(object_names):
    database_object = get_database_object(create=True)
    if database_object is None:
        return
    _set_mapping_value(database_object, OBJECT_NAMES_KEY, object_names)


def register():
    database_object = get_database_object(create=True)
    if database_object is not None:
        _move_to_stagehand_collection(database_object)
        _lock_database_selection(database_object)
    if not bpy.app.timers.is_registered(database_selection_guard):
        bpy.app.timers.register(database_selection_guard, first_interval=DATABASE_SELECT_GUARD_INTERVAL)


def unregister():
    if bpy.app.timers.is_registered(database_selection_guard):
        bpy.app.timers.unregister(database_selection_guard)


def database_selection_guard():
    database_object = get_database_object(create=False)
    if database_object is not None:
        try:
            _lock_database_selection(database_object)
        except RuntimeError:
            pass
    return DATABASE_SELECT_GUARD_INTERVAL
