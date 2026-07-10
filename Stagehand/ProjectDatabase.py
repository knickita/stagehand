import bpy


DATABASE_OBJECT_NAME = "database"
DATABASE_MARKER_KEY = "stagehand_is_database"
DATABASE_VERSION_KEY = "stagehand_database_version"
CONNECTIONS_KEY = "stagehand_connections"
LINK_PARENTS_KEY = "stagehand_link_parents"
OBJECT_NAMES_KEY = "stagehand_object_names"


def _data_objects():
    return getattr(bpy.data, "objects", None)


def _data_scenes():
    return getattr(bpy.data, "scenes", None)


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


def _link_database_to_scene(obj):
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        try:
            scene.collection.objects.link(obj)
            return
        except RuntimeError:
            pass

    scenes = _data_scenes()
    if scenes is None:
        return

    for candidate_scene in scenes:
        try:
            candidate_scene.collection.objects.link(obj)
            return
        except RuntimeError:
            continue


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
    get_database_object(create=True)


def unregister():
    return
