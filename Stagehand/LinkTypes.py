from enum import IntEnum


class StagehandLinkType(IntEnum):
    LITEC30 = 0
    HOOK = 1
    PIPE = 2
    POWER_IN_CEE16A_MONO = 3
    POWER_OUT_CEE16A_MONO = 4
    POWER_IN_POWERCON_BLUE = 5
    POWER_OUT_POWERCON_BLUE = 6
    POWER_IN_POWERCON_WHITE = 7
    POWER_OUT_POWERCON_WHITE = 8
    POWER_IN_POWERCONTRUE = 9
    POWER_OUT_POWERCONTRUE = 10
    POWER_IN_CEE63A_PENTA = 11
    POWER_OUT_CEE63A_PENTA = 12
    LAYHER_ROSETTA = 13
    LAYHER_HOOK = 14
    LAYHER_UP = 15
    LAYHER_DOWN = 16
    YESTECH_MECHANICAL_LEFT = 17
    YESTECH_MECHANICAL_RIGHT = 18
    YESTECH_MECHANICAL_UP = 19
    YESTECH_MECHANICAL_DOWN = 20
    INFILED_MECHANICAL_LEFT = 21
    INFILED_MECHANICAL_RIGHT = 22
    INFILED_MECHANICAL_UP = 23
    INFILED_MECHANICAL_DOWN = 24
    LITEC40 = 25
    LITEC_CARRELLO_TRUSS = 26
    LITEC30_SECTION_OUTER = 27
    LITEC30_SECTION_INNER = 28
    SIXTEMA_JOINT = 29
    SIXTEMA_LEG_SITE = 30
    SIXTEMA_LEG = 31


LINK_COMPATIBILITY = {
    StagehandLinkType.LITEC30: (
        StagehandLinkType.LITEC30,
        StagehandLinkType.LITEC_CARRELLO_TRUSS,
    ),
    StagehandLinkType.HOOK: (StagehandLinkType.PIPE,),
    StagehandLinkType.PIPE: (StagehandLinkType.HOOK,),
    StagehandLinkType.POWER_IN_CEE16A_MONO: (StagehandLinkType.POWER_OUT_CEE16A_MONO,),
    StagehandLinkType.POWER_OUT_CEE16A_MONO: (StagehandLinkType.POWER_IN_CEE16A_MONO,),
    StagehandLinkType.POWER_IN_POWERCON_BLUE: (StagehandLinkType.POWER_OUT_POWERCON_BLUE,),
    StagehandLinkType.POWER_OUT_POWERCON_BLUE: (StagehandLinkType.POWER_IN_POWERCON_BLUE,),
    StagehandLinkType.POWER_IN_POWERCON_WHITE: (StagehandLinkType.POWER_OUT_POWERCON_WHITE,),
    StagehandLinkType.POWER_OUT_POWERCON_WHITE: (StagehandLinkType.POWER_IN_POWERCON_WHITE,),
    StagehandLinkType.POWER_IN_POWERCONTRUE: (StagehandLinkType.POWER_OUT_POWERCONTRUE,),
    StagehandLinkType.POWER_OUT_POWERCONTRUE: (StagehandLinkType.POWER_IN_POWERCONTRUE,),
    StagehandLinkType.POWER_IN_CEE63A_PENTA: (StagehandLinkType.POWER_OUT_CEE63A_PENTA,),
    StagehandLinkType.POWER_OUT_CEE63A_PENTA: (StagehandLinkType.POWER_IN_CEE63A_PENTA,),
    StagehandLinkType.LAYHER_ROSETTA: (StagehandLinkType.LAYHER_HOOK,),
    StagehandLinkType.LAYHER_HOOK: (StagehandLinkType.LAYHER_ROSETTA,),
    StagehandLinkType.LAYHER_UP: (StagehandLinkType.LAYHER_DOWN,),
    StagehandLinkType.LAYHER_DOWN: (StagehandLinkType.LAYHER_UP,),
    StagehandLinkType.YESTECH_MECHANICAL_LEFT: (StagehandLinkType.YESTECH_MECHANICAL_RIGHT,),
    StagehandLinkType.YESTECH_MECHANICAL_RIGHT: (StagehandLinkType.YESTECH_MECHANICAL_LEFT,),
    StagehandLinkType.YESTECH_MECHANICAL_UP: (StagehandLinkType.YESTECH_MECHANICAL_DOWN,),
    StagehandLinkType.YESTECH_MECHANICAL_DOWN: (StagehandLinkType.YESTECH_MECHANICAL_UP,),
    StagehandLinkType.INFILED_MECHANICAL_LEFT: (StagehandLinkType.INFILED_MECHANICAL_RIGHT,),
    StagehandLinkType.INFILED_MECHANICAL_RIGHT: (StagehandLinkType.INFILED_MECHANICAL_LEFT,),
    StagehandLinkType.INFILED_MECHANICAL_UP: (StagehandLinkType.INFILED_MECHANICAL_DOWN,),
    StagehandLinkType.INFILED_MECHANICAL_DOWN: (StagehandLinkType.INFILED_MECHANICAL_UP,),
    StagehandLinkType.LITEC40: (
        StagehandLinkType.LITEC40,
        StagehandLinkType.LITEC_CARRELLO_TRUSS,
    ),
    StagehandLinkType.LITEC_CARRELLO_TRUSS: (
        StagehandLinkType.LITEC30,
        StagehandLinkType.LITEC40,
    ),
    StagehandLinkType.LITEC30_SECTION_OUTER: (StagehandLinkType.LITEC30_SECTION_INNER,),
    StagehandLinkType.LITEC30_SECTION_INNER: (StagehandLinkType.LITEC30_SECTION_OUTER,),
    StagehandLinkType.SIXTEMA_JOINT: (StagehandLinkType.SIXTEMA_JOINT,),
    StagehandLinkType.SIXTEMA_LEG_SITE: (StagehandLinkType.SIXTEMA_LEG,),
    StagehandLinkType.SIXTEMA_LEG: (StagehandLinkType.SIXTEMA_LEG_SITE,),
}


def coerce_link_type(value):
    if isinstance(value, StagehandLinkType):
        return value

    return StagehandLinkType(int(value))


def get_compatible_link_types(link_type):
    link_type = coerce_link_type(link_type)
    return LINK_COMPATIBILITY.get(link_type, ())


def are_link_types_compatible(source_link_type, target_link_type):
    source_link_type = coerce_link_type(source_link_type)
    target_link_type = coerce_link_type(target_link_type)
    return target_link_type in get_compatible_link_types(source_link_type)


def link_type_label(link_type):
    link_type = coerce_link_type(link_type)
    return link_type.name.replace("_", " ").title()
