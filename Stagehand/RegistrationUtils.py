import bpy


def safe_register_class(cls):
    existing_class = getattr(bpy.types, cls.__name__, None)
    if existing_class is not None and existing_class is not cls:
        try:
            bpy.utils.unregister_class(existing_class)
        except (RuntimeError, ValueError):
            pass

    try:
        bpy.utils.register_class(cls)
    except ValueError as exc:
        if "already registered as a subclass" not in str(exc):
            raise


def safe_unregister_class(cls):
    targets = []
    existing_class = getattr(bpy.types, cls.__name__, None)
    if existing_class is not None:
        targets.append(existing_class)
    if cls not in targets:
        targets.append(cls)

    for target in targets:
        try:
            bpy.utils.unregister_class(target)
        except (RuntimeError, ValueError):
            pass


def safe_define_property(owner, name, prop):
    safe_remove_property(owner, name)
    setattr(owner, name, prop)


def safe_remove_property(owner, name):
    if not hasattr(owner, name):
        return

    try:
        delattr(owner, name)
    except (AttributeError, TypeError):
        pass


def safe_remove_keymaps(addon_keymaps):
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError, ValueError):
            pass
    addon_keymaps.clear()


def safe_append_menu(menu, draw_fn):
    safe_remove_menu(menu, draw_fn)
    menu.append(draw_fn)


def safe_remove_menu(menu, draw_fn):
    try:
        menu.remove(draw_fn)
    except (RuntimeError, ValueError):
        pass


def safe_add_handler(handler_list, handler):
    if handler not in handler_list:
        handler_list.append(handler)


def safe_remove_handler(handler_list, handler):
    if handler in handler_list:
        handler_list.remove(handler)
