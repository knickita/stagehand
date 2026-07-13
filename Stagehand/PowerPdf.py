import math
import os
from collections import Counter, defaultdict
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper

from . import Connections
from .LinkTypes import (
    MONOPHASE_POWER_INPUT_LINK_TYPES,
    MONOPHASE_POWER_OUTPUT_LINK_TYPES,
    THREEPHASE_POWER_INPUT_LINK_TYPES,
    THREEPHASE_POWER_OUTPUT_LINK_TYPES,
    link_type_label,
)
from .PowerManagement.scene import generate_power_solution
from .PowerManagement.solver import PowerSolverError
from .RegistrationUtils import safe_register_class, safe_unregister_class


PAGE_WIDTH = 842.0
PAGE_HEIGHT = 595.0
PAGE_MARGIN = 28.0
HEADER_HEIGHT = 48.0
ROW_GAP = 5.0
DIAGRAM_GAP = 7.0
PAGE_CONTENT_TOP = PAGE_HEIGHT - PAGE_MARGIN - HEADER_HEIGHT - 13.0
PAGE_CONTENT_BOTTOM = PAGE_MARGIN + 33.0

SOURCE_X = 40.0
SOURCE_WIDTH = 128.0
POWERBOX_X = 300.0
POWERBOX_WIDTH = 150.0
SINK_X = 655.0
SINK_WIDTH = 153.0
TRUNK_X = 475.0

BOX_LINE_HEIGHT = 11.0
BOX_VERTICAL_PADDING = 8.0
MIN_BOX_HEIGHT = 24.0

NODE_FILL = (0.76, 0.74, 0.74)
NODE_BORDER = (0.26, 0.28, 0.32)
HEADER_COLOR = (0.08, 0.18, 0.32)
TEXT_COLOR = (0.0, 0.0, 0.0)
WARNING_COLOR = (0.72, 0.12, 0.08)

MONOPHASE_INPUT_TYPES = frozenset(map(int, MONOPHASE_POWER_INPUT_LINK_TYPES))
MONOPHASE_OUTPUT_TYPES = frozenset(map(int, MONOPHASE_POWER_OUTPUT_LINK_TYPES))
THREEPHASE_INPUT_TYPES = frozenset(map(int, THREEPHASE_POWER_INPUT_LINK_TYPES))
THREEPHASE_OUTPUT_TYPES = frozenset(map(int, THREEPHASE_POWER_OUTPUT_LINK_TYPES))


def _project_name():
    blend_path = bpy.data.filepath
    return Path(blend_path).stem if blend_path else "Stagehand"


def _power_pdf_filename(project_name):
    sanitized = "".join(
        character if character.isalnum() or character in (" ", "-", "_") else "_"
        for character in project_name
    ).strip()
    return f"{'_'.join(sanitized.split()) or 'stagehand'}_power.pdf"


def _visible_stagehand_objects():
    objects = []
    for obj in bpy.data.objects:
        stagehand = getattr(obj, "stagehand", None)
        if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
            continue
        if not obj.hide_get() and not obj.hide_viewport:
            objects.append(obj)
    return objects


def _object_label(obj):
    catalogue_name = str(getattr(obj.stagehand, "catalogueName", "")).strip()
    return catalogue_name or obj.name_full


def _object_box_lines(obj):
    primary = _object_label(obj)
    lines = [primary]
    if obj.name_full.casefold() != primary.casefold():
        lines.append(obj.name_full)
    return lines


def _link_index(objects):
    index = {}
    for obj in objects:
        object_uid = Connections.get_object_uid(obj)
        for link_index, link in Connections.iter_object_links(obj):
            link_uid = str(link.uid)
            if link_uid:
                index[link_uid] = {
                    "obj": obj,
                    "object_uid": object_uid,
                    "link": link,
                    "link_index": link_index,
                }
    return index


def _route_length(solver, route_edges):
    total = 0.0
    seen_edges = set()
    for first_node, second_node in route_edges:
        edge_key = tuple(sorted((int(first_node), int(second_node))))
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        total += math.dist(solver.nodes[first_node], solver.nodes[second_node])
    return total


def _line_type_label(link):
    label = link_type_label(link.type)
    for prefix in ("Power Out ", "Power In "):
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    return (
        label.replace("Powercontrue", "PowerCON TRUE1")
        .replace("Powercon", "PowerCON")
        .replace("Cee", "CEE ")
        .replace(" Mono", " monofase")
        .replace(" Penta", " pentapolare")
        .replace(" Blue", " blu")
        .replace(" White", " bianco")
    )


def _destination_labels(destination_objects):
    counts = Counter(_object_label(obj) for obj in destination_objects)
    return [
        f"{quantity} x {label}" if quantity > 1 else label
        for label, quantity in sorted(counts.items(), key=lambda item: item[0].casefold())
    ]


def _connection_destinations(generated_connections, link_lookup, output_link_uid, input_types):
    destinations = []
    seen_object_uids = set()
    for input_link_uid, connected_output_uid in generated_connections.items():
        if connected_output_uid != output_link_uid:
            continue
        input_item = link_lookup.get(input_link_uid)
        if input_item is None or int(input_item["link"].type) not in input_types:
            continue
        object_uid = input_item["object_uid"]
        if object_uid not in seen_object_uids:
            seen_object_uids.add(object_uid)
            destinations.append(input_item["obj"])
    return destinations


def _monophase_output_port_number(obj, output_link_index):
    port_number = 0
    for link_index, link in Connections.iter_object_links(obj):
        if int(link.type) not in MONOPHASE_OUTPUT_TYPES:
            continue
        port_number += 1
        if link_index == output_link_index:
            return port_number
    return output_link_index + 1


def _collapse_monophase_lines(raw_lines, powerbox_uids):
    incoming_line_by_object_uid = {}
    for line in raw_lines:
        for object_uid, _obj in line["destination_items"]:
            incoming_line_by_object_uid.setdefault(object_uid, line)

    collapsed_by_origin = {}
    destination_uids_by_origin = defaultdict(set)

    for line in raw_lines:
        origin = line
        visited_line_ids = set()
        while origin["source_uid"] not in powerbox_uids:
            if origin["line_id"] in visited_line_ids:
                origin = None
                break
            visited_line_ids.add(origin["line_id"])
            origin = incoming_line_by_object_uid.get(origin["source_uid"])
            if origin is None:
                break

        if origin is None:
            continue

        origin_key = (
            origin["source_uid"],
            origin["output_link_uid"] or origin["output_index"],
        )
        collapsed = collapsed_by_origin.get(origin_key)
        if collapsed is None:
            collapsed = {
                "source_uid": origin["source_uid"],
                "length": 0.0,
                "type": origin["type"],
                "destinations": [],
                "output_index": origin["output_index"],
                "port_number": origin["port_number"],
            }
            collapsed_by_origin[origin_key] = collapsed

        collapsed["length"] += line["length"]
        seen_destination_uids = destination_uids_by_origin[origin_key]
        for object_uid, obj in line["destination_items"]:
            if object_uid in seen_destination_uids:
                continue
            seen_destination_uids.add(object_uid)
            collapsed["destinations"].append(obj)

    return list(collapsed_by_origin.values())


def _order_diagrams_upstream_first(diagrams):
    diagrams_by_uid = {
        Connections.get_object_uid(diagram["powerbox"]): diagram
        for diagram in diagrams
    }
    children_by_uid = defaultdict(list)
    root_uids = []

    for object_uid, diagram in diagrams_by_uid.items():
        incoming = diagram["incoming"]
        parent_uid = (
            Connections.get_object_uid(incoming["source"])
            if incoming is not None
            else None
        )
        if parent_uid in diagrams_by_uid and parent_uid != object_uid:
            children_by_uid[parent_uid].append(object_uid)
        else:
            root_uids.append(object_uid)

    def sort_key(object_uid):
        powerbox = diagrams_by_uid[object_uid]["powerbox"]
        return (
            _object_label(powerbox).casefold(),
            powerbox.name_full.casefold(),
            object_uid,
        )

    ordered = []
    visited = set()

    def append_levels(start_uids):
        level_uids = sorted(set(start_uids) - visited, key=sort_key)
        while level_uids:
            next_level_uids = []
            for object_uid in level_uids:
                visited.add(object_uid)
                ordered.append(diagrams_by_uid[object_uid])
                next_level_uids.extend(children_by_uid.get(object_uid, ()))
            level_uids = sorted(set(next_level_uids) - visited, key=sort_key)

    append_levels(root_uids)
    append_levels(diagrams_by_uid)
    return ordered


def _build_power_diagrams(result):
    objects = _visible_stagehand_objects()
    objects_by_uid = {Connections.get_object_uid(obj): obj for obj in objects}
    link_lookup = _link_index(objects)
    incoming_by_object_uid = {}
    raw_monophase_lines = []
    threephase_inputs = THREEPHASE_INPUT_TYPES
    monophase_inputs = MONOPHASE_INPUT_TYPES

    for line_id, assignment in result.threephase_output_assignments.items():
        output_item = link_lookup.get(assignment.output_link_uid)
        if output_item is None:
            continue
        destinations = _connection_destinations(
            result.generated_powerline_connections,
            link_lookup,
            assignment.output_link_uid,
            threephase_inputs,
        )
        entry = {
            "source": output_item["obj"],
            "length": _route_length(result.solver, result.threephase_routes.get(line_id, ())),
            "type": _line_type_label(output_item["link"]),
        }
        for destination in destinations:
            incoming_by_object_uid[Connections.get_object_uid(destination)] = entry

    for line_id, assignment in result.power_line_output_assignments.items():
        output_item = link_lookup.get(assignment.output_link_uid)
        if output_item is None:
            continue
        destinations = _connection_destinations(
            result.generated_powerline_connections,
            link_lookup,
            assignment.output_link_uid,
            monophase_inputs,
        )
        raw_monophase_lines.append({
            "line_id": line_id,
            "source_uid": output_item["object_uid"],
            "output_link_uid": assignment.output_link_uid,
            "length": _route_length(result.solver, result.power_line_routes.get(line_id, ())),
            "type": _line_type_label(output_item["link"]),
            "destination_items": [
                (Connections.get_object_uid(destination), destination)
                for destination in destinations
            ],
            "output_index": output_item["link_index"],
            "port_number": _monophase_output_port_number(
                output_item["obj"],
                output_item["link_index"],
            ),
        })

    powerbox_uids = set(incoming_by_object_uid)
    for obj in objects:
        link_types = {int(link.type) for _index, link in Connections.iter_object_links(obj)}
        if (
            link_types & THREEPHASE_INPUT_TYPES
            or (
                link_types & THREEPHASE_OUTPUT_TYPES
                and link_types & MONOPHASE_OUTPUT_TYPES
            )
        ):
            powerbox_uids.add(Connections.get_object_uid(obj))

    output_lines_by_object_uid = defaultdict(list)
    for line in _collapse_monophase_lines(raw_monophase_lines, powerbox_uids):
        output_lines_by_object_uid[line["source_uid"]].append(line)

    diagrams = []
    for source_uid in powerbox_uids:
        source_obj = objects_by_uid.get(source_uid)
        if source_obj is None:
            continue
        diagrams.append({
            "powerbox": source_obj,
            "incoming": incoming_by_object_uid.get(source_uid),
            "lines": sorted(
                output_lines_by_object_uid.get(source_uid, ()),
                key=lambda line: (line["port_number"], line["length"]),
            ),
        })
    return _order_diagrams_upstream_first(diagrams)


def _safe_text(value):
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _pdf_escape(value):
    return _safe_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_text(font, size, x, y, text, color=TEXT_COLOR):
    return (
        f"BT {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
        f"/{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({_pdf_escape(text)}) Tj ET"
    )


def _wrap_text(text, max_characters):
    raw_words = str(text).split()
    if not raw_words:
        return [""]
    words = []
    for word in raw_words:
        words.extend(
            word[start:start + max_characters]
            for start in range(0, len(word), max_characters)
        )
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_characters:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _wrapped_box_lines(lines, width):
    wrapped = []
    max_characters = max(12, int(width / 5.8))
    for line in lines:
        wrapped.extend(_wrap_text(line, max_characters))
    return wrapped or [""]


def _box_required_height(lines, width):
    text_height = len(_wrapped_box_lines(lines, width)) * BOX_LINE_HEIGHT
    return max(MIN_BOX_HEIGHT, text_height + BOX_VERTICAL_PADDING)


def _box_commands(x, y, width, height, lines, bold_first=True):
    commands = [
        "q",
        f"{NODE_FILL[0]:.3f} {NODE_FILL[1]:.3f} {NODE_FILL[2]:.3f} rg",
        f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f",
        f"{NODE_BORDER[0]:.3f} {NODE_BORDER[1]:.3f} {NODE_BORDER[2]:.3f} RG 0.8 w",
        f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re S",
        "Q",
    ]
    wrapped = _wrapped_box_lines(lines, width)
    line_height = BOX_LINE_HEIGHT
    baseline = y + ((height + (len(wrapped) * line_height)) * 0.5) - line_height
    for index, line in enumerate(wrapped):
        font = "F2" if bold_first and index == 0 else "F1"
        size = 9.0 if index == 0 else 8.0
        approximate_width = len(line) * size * 0.48
        text_x = x + max(5.0, (width - approximate_width) * 0.5)
        commands.append(_pdf_text(font, size, text_x, baseline - (index * line_height), line))
    return commands


def _format_length(length):
    return f"{length * 100.0:.0f} cm" if length < 1.0 else f"{length:.1f} metri"


def _page_header(project_name, page_number, page_count, continuation=False):
    title = "Schema unifilare di distribuzione elettrica"
    if continuation:
        title += " - continua"
    return [
        "q",
        f"{HEADER_COLOR[0]:.3f} {HEADER_COLOR[1]:.3f} {HEADER_COLOR[2]:.3f} RG 1.6 w",
        f"{PAGE_MARGIN:.2f} {PAGE_MARGIN:.2f} {PAGE_WIDTH - (PAGE_MARGIN * 2):.2f} {PAGE_HEIGHT - (PAGE_MARGIN * 2):.2f} re S",
        f"{PAGE_MARGIN:.2f} {PAGE_HEIGHT - PAGE_MARGIN - HEADER_HEIGHT:.2f} m "
        f"{PAGE_WIDTH - PAGE_MARGIN:.2f} {PAGE_HEIGHT - PAGE_MARGIN - HEADER_HEIGHT:.2f} l S",
        "Q",
        _pdf_text("F2", 16, PAGE_MARGIN + 10.0, PAGE_HEIGHT - PAGE_MARGIN - 28.0, title),
        _pdf_text("F1", 9, PAGE_WIDTH - PAGE_MARGIN - 190.0, PAGE_HEIGHT - PAGE_MARGIN - 27.0, project_name),
        _pdf_text("F1", 8, PAGE_WIDTH - PAGE_MARGIN - 64.0, PAGE_MARGIN + 8.0, f"{page_number}/{page_count}"),
    ]


def _diagram_source_content(diagram):
    incoming = diagram["incoming"]
    if incoming is None:
        return ["Alimentazione", "non assegnata"], "Linea in ingresso non disponibile"
    return (
        _object_box_lines(incoming["source"]),
        f"{incoming['type']} | {incoming['length']:.1f} m",
    )


def _line_destination_lines(line):
    return _destination_labels(line["destinations"]) or ["Destinazione non assegnata"]


def _line_sink_height(line):
    return _box_required_height(_line_destination_lines(line), SINK_WIDTH)


def _diagram_block_height(diagram, page_lines):
    source_lines, _incoming_label = _diagram_source_content(diagram)
    source_height = _box_required_height(source_lines, SOURCE_WIDTH)
    powerbox_height = _box_required_height(
        _object_box_lines(diagram["powerbox"]),
        POWERBOX_WIDTH,
    )
    if not page_lines:
        return max(source_height, powerbox_height, MIN_BOX_HEIGHT)

    sink_heights = [_line_sink_height(line) for line in page_lines]
    rows_height = sum(sink_heights) + (ROW_GAP * (len(sink_heights) - 1))
    return max(source_height, powerbox_height, rows_height)


def _line_row_layout(page_lines, graph_center_y):
    sink_heights = [_line_sink_height(line) for line in page_lines]
    rows_height = sum(sink_heights) + (ROW_GAP * max(0, len(sink_heights) - 1))
    cursor_top = graph_center_y + (rows_height * 0.5)
    rows = []
    for line, sink_height in zip(page_lines, sink_heights):
        row_y = cursor_top - (sink_height * 0.5)
        rows.append((row_y, sink_height, line))
        cursor_top -= sink_height + ROW_GAP
    return rows


def _diagram_page_stream(
    project_name,
    diagram,
    page_lines,
    page_number,
    page_count,
    continuation=False,
    warnings=(),
    top_y=PAGE_CONTENT_TOP,
    include_header=True,
):
    commands = (
        _page_header(project_name, page_number, page_count, continuation=continuation)
        if include_header
        else []
    )
    block_height = _diagram_block_height(diagram, page_lines)
    graph_center_y = top_y - (block_height * 0.5)
    source_lines, incoming_label = _diagram_source_content(diagram)
    source_height = _box_required_height(source_lines, SOURCE_WIDTH)
    powerbox_lines = _object_box_lines(diagram["powerbox"])
    powerbox_height = _box_required_height(powerbox_lines, POWERBOX_WIDTH)

    commands.extend(_box_commands(
        SOURCE_X,
        graph_center_y - (source_height * 0.5),
        SOURCE_WIDTH,
        source_height,
        source_lines,
    ))
    commands.extend(_box_commands(
        POWERBOX_X,
        graph_center_y - (powerbox_height * 0.5),
        POWERBOX_WIDTH,
        powerbox_height,
        powerbox_lines,
    ))
    commands.extend([
        f"0 0 0 RG 0.9 w {SOURCE_X + SOURCE_WIDTH:.2f} {graph_center_y:.2f} m {POWERBOX_X:.2f} {graph_center_y:.2f} l S",
        _pdf_text("F1", 7, SOURCE_X + SOURCE_WIDTH + 6.0, graph_center_y + 7.0, incoming_label),
    ])

    if not page_lines:
        commands.append(_pdf_text(
            "F1",
            9,
            TRUNK_X + 18.0,
            graph_center_y,
            "Nessuna linea in uscita assegnata",
        ))
    else:
        rows = _line_row_layout(page_lines, graph_center_y)
        row_centers = [row_y for row_y, _height, _line in rows]
        commands.append(
            f"0 0 0 RG 0.9 w {POWERBOX_X + POWERBOX_WIDTH:.2f} {graph_center_y:.2f} m {TRUNK_X:.2f} {graph_center_y:.2f} l S"
        )
        if len(row_centers) > 1:
            commands.append(
                f"0 0 0 RG 0.9 w {TRUNK_X:.2f} {row_centers[-1]:.2f} m {TRUNK_X:.2f} {row_centers[0]:.2f} l S"
            )
        for row_y, sink_height, line in rows:
            commands.append(
                f"0 0 0 RG 0.9 w {TRUNK_X:.2f} {row_y:.2f} m {SINK_X:.2f} {row_y:.2f} l S"
            )
            line_label = (
                f"Porta {line['port_number']} - {line['type']} - "
                f"{_format_length(line['length'])}"
            )
            commands.append(_pdf_text("F1", 8, TRUNK_X + 10.0, row_y + 7.0, line_label))
            commands.extend(_box_commands(
                SINK_X,
                row_y - (sink_height * 0.5),
                SINK_WIDTH,
                sink_height,
                _line_destination_lines(line),
                bold_first=False,
            ))

    if warnings:
        warning_text = "Avvisi: " + "; ".join(warnings)
        for index, warning in enumerate(_wrap_text(warning_text, 120)[:2]):
            commands.append(_pdf_text(
                "F1",
                7.5,
                PAGE_MARGIN + 10.0,
                PAGE_MARGIN + 9.0 + (index * 9.0),
                warning,
                color=WARNING_COLOR,
            ))
    return "\n".join(commands)


def _power_page_streams(project_name, diagrams, warnings=()):
    pages = []
    current_page = []
    cursor_top = PAGE_CONTENT_TOP

    for diagram in diagrams:
        lines = diagram["lines"]
        if not lines:
            block_height = _diagram_block_height(diagram, ())
            if current_page and block_height > cursor_top - PAGE_CONTENT_BOTTOM:
                pages.append(current_page)
                current_page = []
                cursor_top = PAGE_CONTENT_TOP
            current_page.append((diagram, [], False, cursor_top))
            cursor_top -= block_height + DIAGRAM_GAP
            continue

        start = 0
        while start < len(lines):
            remaining_height = cursor_top - PAGE_CONTENT_BOTTOM
            best_end = start
            best_height = 0.0
            end = start + 1
            while end <= len(lines):
                candidate_height = _diagram_block_height(diagram, lines[start:end])
                if candidate_height <= remaining_height or (
                    not current_page and end == start + 1
                ):
                    best_end = end
                    best_height = candidate_height
                    end += 1
                    continue
                break

            if best_end == start:
                pages.append(current_page)
                current_page = []
                cursor_top = PAGE_CONTENT_TOP
                continue

            current_page.append((diagram, lines[start:best_end], start > 0, cursor_top))
            cursor_top -= best_height + DIAGRAM_GAP
            start = best_end

    if current_page:
        pages.append(current_page)

    page_count = len(pages)
    page_streams = []
    for page_number, page_specs in enumerate(pages, start=1):
        page_continues = any(
            continuation
            for _diagram, _lines, continuation, _top_y in page_specs
        )
        diagram_streams = []
        for index, page_spec in enumerate(page_specs):
            diagram, page_lines, _continuation, top_y = page_spec
            diagram_streams.append(_diagram_page_stream(
                project_name,
                diagram,
                page_lines,
                page_number,
                page_count,
                continuation=page_continues,
                warnings=warnings if index == 0 else (),
                top_y=top_y,
                include_header=index == 0,
            ))
        page_streams.append("\n".join(diagram_streams))
    return page_streams


def _pdf_stream(data):
    encoded = data.encode("latin-1", "replace")
    return (
        b"<< /Length "
        + str(len(encoded)).encode("ascii")
        + b" >>\nstream\n"
        + encoded
        + b"\nendstream"
    )


def _write_pdf(filepath, page_streams):
    object_entries = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    page_object_numbers = []
    pending_pages = []
    next_object_number = 5
    for content_stream in page_streams:
        page_object_numbers.append(next_object_number)
        pending_pages.append((next_object_number + 1, content_stream))
        next_object_number += 2

    page_refs = " ".join(f"{number} 0 R" for number in page_object_numbers)
    object_entries[1] = (
        f"<< /Type /Pages /Kids [{page_refs}] /Count {len(page_object_numbers)} >>"
    ).encode("ascii")
    for content_object_number, content_stream in pending_pages:
        object_entries.append((
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_object_number} 0 R >>"
        ).encode("ascii"))
        object_entries.append(_pdf_stream(content_stream))

    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(object_entries, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(object_entries) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend((
        f"trailer\n<< /Size {len(object_entries) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii"))
    with open(filepath, "wb") as handle:
        handle.write(output)


class STAGEHAND_OT_generate_pdf_power(bpy.types.Operator, ExportHelper):
    bl_idname = "stagehand.generate_pdf_power"
    bl_label = "Generate PDF Power"
    bl_description = "Generate a PDF diagram of powerboxes, cable lengths, and connected equipment"
    bl_options = {'REGISTER'}

    filename_ext = ".pdf"
    filter_glob: bpy.props.StringProperty(default="*.pdf", options={'HIDDEN'})

    def invoke(self, context, event):
        if not self.filepath:
            blend_path = bpy.data.filepath
            base_directory = Path(blend_path).parent if blend_path else Path.home()
            self.filepath = str(base_directory / _power_pdf_filename(_project_name()))
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            result = generate_power_solution(context)
            diagrams = _build_power_diagrams(result)
            if not diagrams:
                self.report({'ERROR'}, "No powerboxes with electrical links were found")
                return {'CANCELLED'}
            _write_pdf(
                self.filepath,
                _power_page_streams(_project_name(), diagrams, warnings=result.warnings),
            )
        except PowerSolverError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate power PDF: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Power PDF created: {os.path.basename(self.filepath)}")
        return {'FINISHED'}


classes = (STAGEHAND_OT_generate_pdf_power,)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
