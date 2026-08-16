"""Blender UI and Shape Key bake pipeline for the Koromo solver DLL."""

from array import array
import json
import math
import tempfile

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from .i18n import tr, translations_dict
from .housei import HouReadError, build_combined_shell, read_hou_plan
from .native import NativeSolverError, Vec3, get_library


_BAKE_TAG = "koromo_bake_version"
_PREPARED_COLLECTION_TAG = "koromo_prepared_collection_version"
_PREPARED_OBJECT_TAG = "koromo_prepared_object_version"
_PREPARED_ROLE_TAG = "koromo_role"
_PREPARED_SOURCE_TAG = "koromo_source"
_PREPARED_SOURCE_MODE_TAG = "koromo_source_mode"
_PREPARED_HOU_DIGEST_TAG = "koromo_hou_plan_digest"
_PREPARED_SEAMS_TAG = "koromo_seam_pairs"
_PREPARED_SEAM_DISTANCE_TAG = "koromo_seam_distance"
_PREPARED_SEAM_ENABLED_TAG = "koromo_seam_enabled"
_PREPARED_SEAM_ATTRIBUTE_TAG = "koromo_seam_attribute"
_PREPARED_SEAM_SOURCE_TAG = "koromo_seam_source"
_PREPARED_CROP_ENABLED_TAG = "koromo_static_crop_enabled"
_PREPARED_CROP_MIN_TAG = "koromo_static_crop_min_z"
_PREPARED_CROP_MAX_TAG = "koromo_static_crop_max_z"
_PREPARED_COLLECTION_NAME = "Koromo Simulation"
_PREPARED_VERSION = 3
_STATIC_TWICE_AREA_FILTER = 1.25e-7
_STATIC_CROP_GROUP = "KOROMO_STATIC_CROP"
_STATIC_CROP_MODIFIER = "Koromo Static Crop"
_DEFAULT_SEAM_ATTRIBUTE = "yohsai_zozo_stitch"
_ADAPTIVE_EDGE_PERCENTILE = 0.10
_ADAPTIVE_EDGE_FRACTION = 0.50
_BAKE_RUNNING = False


def _mesh_object(_self, obj) -> bool:
    return obj is not None and obj.type == "MESH"


def _triangulated_mesh(mesh, matrix_world):
    mesh.calc_loop_triangles()
    vertices = [tuple(matrix_world @ vertex.co) for vertex in mesh.vertices]
    triangles = [tuple(triangle.vertices) for triangle in mesh.loop_triangles]
    return vertices, triangles


def _shell_mesh(obj):
    return _triangulated_mesh(obj.data, obj.matrix_world)


def _static_topology_signature(mesh):
    return (
        len(mesh.vertices),
        len(mesh.edges),
        len(mesh.polygons),
        len(mesh.loops),
    )


def _filtered_static_mesh(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    if mesh is None:
        raise RuntimeError("BODY object could not be evaluated as a mesh")
    try:
        vertices, triangles = _triangulated_mesh(mesh, evaluated.matrix_world)
        accepted = []
        skipped = 0
        for i0, i1, i2 in triangles:
            a = vertices[i0]
            b = vertices[i1]
            c = vertices[i2]
            ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            cross = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            twice_area = math.sqrt(sum(value * value for value in cross))
            if (
                math.isfinite(twice_area)
                and twice_area > _STATIC_TWICE_AREA_FILTER
            ):
                accepted.append((i0, i1, i2))
            else:
                skipped += 1
        return vertices, accepted, skipped, _static_topology_signature(mesh)
    finally:
        evaluated.to_mesh_clear()


def _evaluated_static_vertices(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    if mesh is None:
        raise RuntimeError("Animated BODY could not be evaluated as a mesh")
    try:
        vertices = [
            tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices
        ]
        return vertices, _static_topology_signature(mesh)
    finally:
        evaluated.to_mesh_clear()


def _evaluated_snapshot(source, depsgraph, name):
    evaluated = source.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(
        evaluated,
        preserve_all_data_layers=True,
        depsgraph=depsgraph,
    )
    if mesh is None:
        raise RuntimeError(
            tr("{name} could not be evaluated as a mesh", name=source.name)
        )
    try:
        mesh.name = f"{name}_Mesh"
        mesh.transform(evaluated.matrix_world)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    obj.color = tuple(source.color)
    obj.show_in_front = source.show_in_front
    return obj


def _animated_static_copy(source, name):
    """Copy the object stack while sharing untouched source mesh data."""
    obj = source.copy()
    obj.name = name
    obj.hide_viewport = False
    obj.hide_render = True
    obj.display_type = "WIRE"
    obj.show_in_front = True
    return obj


def _configure_static_crop(obj, depsgraph, minimum_z, maximum_z):
    """Add a topology-stable final Mask selected in frame-one world space."""
    if not minimum_z < maximum_z:
        raise RuntimeError("STATIC crop minimum must be below its maximum")
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    if mesh is None:
        raise RuntimeError("STATIC could not be evaluated for cropping")
    try:
        if len(mesh.vertices) != len(obj.data.vertices):
            raise RuntimeError(
                "STATIC crop requires deformation-only source modifiers"
            )
        world_z = [
            float((evaluated.matrix_world @ vertex.co).z)
            for vertex in mesh.vertices
        ]
        selected = set()
        kept_polygons = 0
        for polygon in mesh.polygons:
            heights = [world_z[index] for index in polygon.vertices]
            if min(heights) <= maximum_z and max(heights) >= minimum_z:
                selected.update(int(index) for index in polygon.vertices)
                kept_polygons += 1
        polygon_count = len(mesh.polygons)
    finally:
        evaluated.to_mesh_clear()
    if not selected or kept_polygons == 0:
        raise RuntimeError("STATIC crop box contains no body polygons")

    # Vertex-group weights live in mesh custom data. Isolate the prepared copy
    # so adding the crop never touches the user's source body.
    obj.data = obj.data.copy()
    group = obj.vertex_groups.get(_STATIC_CROP_GROUP)
    if group is None:
        group = obj.vertex_groups.new(name=_STATIC_CROP_GROUP)
    group.add(sorted(selected), 1.0, "REPLACE")
    modifier = obj.modifiers.new(name=_STATIC_CROP_MODIFIER, type="MASK")
    modifier.vertex_group = group.name
    modifier.invert_vertex_group = False
    return len(selected), len(obj.data.vertices), kept_polygons, polygon_count


def _is_prepared_object(obj) -> bool:
    return bool(obj is not None and obj.get(_PREPARED_OBJECT_TAG, 0))


def _is_prepared_collection(collection) -> bool:
    return bool(collection is not None and collection.get(_PREPARED_COLLECTION_TAG, 0))


def _validate_shell_mesh(mesh) -> None:
    mesh.calc_loop_triangles()
    used_vertices = set()
    rejected = 0
    for triangle in mesh.loop_triangles:
        i0, i1, i2 = triangle.vertices
        used_vertices.update((i0, i1, i2))
        a = mesh.vertices[i0].co
        b = mesh.vertices[i1].co
        c = mesh.vertices[i2].co
        twice_area = (b - a).cross(c - a).length
        if not math.isfinite(twice_area) or twice_area <= 1.0e-7:
            rejected += 1
    if rejected:
        raise RuntimeError(
            tr(
                "Prepared SHELL contains {count} triangles that are too small",
                count=rejected,
            )
        )
    orphan_count = len(mesh.vertices) - len(used_vertices)
    if orphan_count:
        raise RuntimeError(
            tr(
                "Prepared SHELL contains {count} vertices outside its faces",
                count=orphan_count,
            )
        )


def _detect_seam_pairs(mesh, max_distance: float) -> list[tuple[int, int]]:
    """Greedily pair nearby boundary vertices from disconnected components."""
    if not (max_distance > 0.0):
        return []

    coordinates = [vertex.co.copy() for vertex in mesh.vertices]
    adjacency = [set() for _vertex in coordinates]
    edge_counts = {}
    for polygon in mesh.polygons:
        vertices = list(polygon.vertices)
        for index, a in enumerate(vertices):
            b = vertices[(index + 1) % len(vertices)]
            edge = (a, b) if a < b else (b, a)
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
            adjacency[a].add(b)
            adjacency[b].add(a)

    components = [-1] * len(coordinates)
    component = 0
    for root in range(len(coordinates)):
        if components[root] >= 0:
            continue
        components[root] = component
        stack = [root]
        while stack:
            vertex = stack.pop()
            for neighbor in adjacency[vertex]:
                if components[neighbor] < 0:
                    components[neighbor] = component
                    stack.append(neighbor)
        component += 1

    boundary = sorted(
        {vertex for edge, count in edge_counts.items() if count == 1 for vertex in edge}
    )
    cell_size = max_distance
    grid = {}
    for vertex in boundary:
        point = coordinates[vertex]
        cell = tuple(math.floor(float(point[axis]) / cell_size) for axis in range(3))
        grid.setdefault(cell, []).append(vertex)

    maximum_squared = max_distance * max_distance
    candidates = []
    for a in boundary:
        point = coordinates[a]
        cell = tuple(math.floor(float(point[axis]) / cell_size) for axis in range(3))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    neighbor_cell = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
                    for b in grid.get(neighbor_cell, ()):
                        if b <= a or components[a] == components[b]:
                            continue
                        distance_squared = (coordinates[a] - coordinates[b]).length_squared
                        if distance_squared <= maximum_squared:
                            candidates.append((distance_squared, a, b))

    candidates.sort()
    paired = set()
    seams = []
    for _distance_squared, a, b in candidates:
        if a in paired or b in paired:
            continue
        paired.add(a)
        paired.add(b)
        seams.append((a, b))
    return seams


def _marked_seam_pairs(mesh, attribute_name: str):
    """Return explicit EDGE pairs, or None when the named attribute is absent."""
    attribute_name = attribute_name.strip()
    if not attribute_name:
        return None
    attribute = mesh.attributes.get(attribute_name)
    if attribute is None:
        return None
    if attribute.domain != "EDGE" or attribute.data_type != "BOOLEAN":
        raise RuntimeError(
            tr(
                "Seam attribute {name!r} must be a Boolean EDGE attribute",
                name=attribute_name,
            )
        )

    pairs = []
    keys = set()
    for edge in mesh.edges:
        if not attribute.data[edge.index].value:
            continue
        a, b = (int(index) for index in edge.vertices)
        key = (a, b) if a < b else (b, a)
        if key in keys:
            raise RuntimeError(
                tr(
                    "Seam attribute {name!r} has a duplicate edge",
                    name=attribute_name,
                )
            )
        keys.add(key)
        pairs.append(key)
    if not pairs:
        raise RuntimeError(
            tr("Seam attribute {name!r} has no marked edges", name=attribute_name)
        )
    return pairs


def _seam_pairs(mesh, attribute_name: str, max_distance: float):
    marked = _marked_seam_pairs(mesh, attribute_name)
    if marked is not None:
        return marked, tr("attribute {name}", name=attribute_name.strip())
    return _detect_seam_pairs(mesh, max_distance), tr(
        "distance <= {distance:g} m", distance=max_distance
    )


def _prepared_seam_pairs(shell) -> list[tuple[int, int]]:
    flattened = list(shell.get(_PREPARED_SEAMS_TAG, ()))
    if len(flattened) % 2:
        raise RuntimeError("Prepared SHELL seam data is invalid; run Prepare again")
    return [
        (int(flattened[index]), int(flattened[index + 1]))
        for index in range(0, len(flattened), 2)
    ]


def _restore_source_shell_visibility(settings) -> None:
    if settings.source_shell_hidden_by_prepare:
        source = bpy.data.objects.get(settings.prepared_source_shell_name)
        if source is None:
            source = settings.shell_object
        if source is not None:
            source.hide_set(False)
        settings.source_shell_hidden_by_prepare = False

    raw = settings.prepared_source_hou_visibility_json
    if raw:
        try:
            states = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            states = []
        for name, hidden in states:
            obj = bpy.data.objects.get(str(name))
            if obj is not None:
                obj.hide_set(bool(hidden))
        settings.prepared_source_hou_visibility_json = ""


def _hide_hou_source_parts(settings, parts) -> None:
    states = [[obj.name, bool(obj.hide_get())] for obj in parts]
    settings.prepared_source_hou_visibility_json = json.dumps(states)
    for obj in parts:
        obj.hide_set(True)


def _remove_prepared(settings, *, restore_visibility: bool) -> bool:
    if restore_visibility:
        _restore_source_shell_visibility(settings)

    collection = settings.prepared_collection
    objects = []
    for obj in (settings.prepared_shell_object, settings.prepared_static_object):
        if _is_prepared_object(obj):
            objects.append(obj)
    if _is_prepared_collection(collection):
        for obj in collection.objects:
            if _is_prepared_object(obj) and obj not in objects:
                objects.append(obj)

    settings.prepared_shell_object = None
    settings.prepared_static_object = None
    settings.prepared_collection = None
    settings.prepared_source_shell_name = ""
    settings.prepared_source_static_name = ""
    settings.prepared_source_hou_visibility_json = ""

    removed = bool(objects)
    for obj in objects:
        mesh = obj.data if obj.type == "MESH" else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    if (
        _is_prepared_collection(collection)
        and not collection.objects
        and not collection.children
    ):
        bpy.data.collections.remove(collection, do_unlink=True)
        removed = True
    return removed


def _prepared_pair(settings):
    shell = settings.prepared_shell_object
    static = settings.prepared_static_object
    if shell is None or static is None:
        raise RuntimeError("Run Prepare Simulation Copies first")
    if (
        shell.get(_PREPARED_OBJECT_TAG) != _PREPARED_VERSION
        or shell.get(_PREPARED_ROLE_TAG) != "SHELL"
        or static.get(_PREPARED_OBJECT_TAG) != _PREPARED_VERSION
        or static.get(_PREPARED_ROLE_TAG) != "STATIC"
    ):
        raise RuntimeError("Prepared simulation objects are invalid; run Prepare again")
    source_mode = str(shell.get(_PREPARED_SOURCE_MODE_TAG, "OBJECT"))
    if source_mode != settings.source_mode or settings.static_object is None:
        raise RuntimeError("Source objects changed; run Prepare again")
    if static.get(_PREPARED_SOURCE_TAG) != settings.static_object.name:
        raise RuntimeError("Source objects changed; run Prepare again")
    if source_mode == "HOU":
        try:
            plan = read_hou_plan(settings.hou_collection)
        except HouReadError as exc:
            raise RuntimeError(str(exc)) from exc
        if (
            shell.get(_PREPARED_SOURCE_TAG) != plan.collection.name
            or shell.get(_PREPARED_HOU_DIGEST_TAG) != plan.digest
        ):
            raise RuntimeError("HOU sewing plan changed; run Prepare again")
    elif (
        settings.shell_object is None
        or shell.get(_PREPARED_SOURCE_TAG) != settings.shell_object.name
    ):
        raise RuntimeError("Source objects changed; run Prepare again")
    if (
        bool(shell.get(_PREPARED_SEAM_ENABLED_TAG, False))
        != settings.seam_enabled
    ):
        raise RuntimeError("Seam detection setting changed; run Prepare again")
    if source_mode != "HOU" and settings.seam_enabled and not math.isclose(
        float(shell.get(_PREPARED_SEAM_DISTANCE_TAG, -1.0)),
        settings.seam_search_distance,
        rel_tol=1.0e-6,
        abs_tol=1.0e-9,
    ):
        raise RuntimeError("Seam Distance changed; run Prepare again")
    if source_mode != "HOU" and settings.seam_enabled and (
        str(shell.get(_PREPARED_SEAM_ATTRIBUTE_TAG, ""))
        != settings.seam_attribute.strip()
    ):
        raise RuntimeError("Seam Attribute changed; run Prepare again")
    if bool(static.get(_PREPARED_CROP_ENABLED_TAG, False)) != settings.static_crop_enabled:
        raise RuntimeError("STATIC crop setting changed; run Prepare again")
    if settings.static_crop_enabled and (
        not math.isclose(
            float(static.get(_PREPARED_CROP_MIN_TAG, math.nan)),
            settings.static_crop_min_z,
            rel_tol=1.0e-6,
            abs_tol=1.0e-9,
        )
        or not math.isclose(
            float(static.get(_PREPARED_CROP_MAX_TAG, math.nan)),
            settings.static_crop_max_z,
            rel_tol=1.0e-6,
            abs_tol=1.0e-9,
        )
    ):
        raise RuntimeError("STATIC crop range changed; run Prepare again")
    return shell, static


def _owned_bake(obj) -> bool:
    keys = obj.data.shape_keys
    return bool(keys and keys.get(_BAKE_TAG) == 1)


def _clear_owned_bake(obj) -> bool:
    if not obj.data.shape_keys:
        return False
    if not _owned_bake(obj):
        raise RuntimeError("SHELL has Shape Keys that are not owned by Koromo")
    obj.shape_key_clear()
    return True


def _world_to_local_flat(matrix, positions):
    rows = [[float(matrix[row][column]) for column in range(4)] for row in range(3)]
    flattened = [0.0] * (len(positions) * 3)
    for index, (x, y, z) in enumerate(positions):
        offset = index * 3
        flattened[offset] = rows[0][0] * x + rows[0][1] * y + rows[0][2] * z + rows[0][3]
        flattened[offset + 1] = (
            rows[1][0] * x + rows[1][1] * y + rows[1][2] * z + rows[1][3]
        )
        flattened[offset + 2] = (
            rows[2][0] * x + rows[2][1] * y + rows[2][2] * z + rows[2][3]
        )
    return flattened


def _adaptive_motion_threshold(vertices, triangles) -> float:
    """Return a robust half-edge motion limit in world-space metres."""
    edges = set()
    for triangle in triangles:
        for a, b in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            edges.add((a, b) if a < b else (b, a))
    lengths = []
    for a, b in edges:
        distance = math.dist(vertices[a], vertices[b])
        if math.isfinite(distance) and distance > 1.0e-9:
            lengths.append(distance)
    if not lengths:
        raise RuntimeError("SHELL has no usable edges for adaptive substeps")
    lengths.sort()
    index = int(_ADAPTIVE_EDGE_PERCENTILE * (len(lengths) - 1))
    return lengths[index] * _ADAPTIVE_EDGE_FRACTION


def _maximum_vertex_motion(previous, current) -> float:
    if len(previous) != len(current):
        raise RuntimeError(
            "Animated STATIC vertex count changed; use deformation-only modifiers"
        )
    maximum_squared = 0.0
    for before, after in zip(previous, current):
        dx = after[0] - before[0]
        dy = after[1] - before[1]
        dz = after[2] - before[2]
        maximum_squared = max(maximum_squared, dx * dx + dy * dy + dz * dz)
    return math.sqrt(maximum_squared)


def _adaptive_step_calls(
    motion: float,
    motion_threshold: float,
    base_substeps: int,
    maximum_substeps: int,
) -> tuple[int, bool]:
    """Return solver calls and whether the requested effective count was capped."""
    base_substeps = max(1, int(base_substeps))
    maximum_substeps = max(base_substeps, int(maximum_substeps))
    maximum_calls = max(1, maximum_substeps // base_substeps)
    required_substeps = max(
        base_substeps,
        int(math.ceil(max(0.0, motion) / max(motion_threshold, 1.0e-9))),
    )
    requested_calls = max(1, int(math.ceil(required_substeps / base_substeps)))
    return min(requested_calls, maximum_calls), requested_calls > maximum_calls


def _set_linear_interpolation(action) -> None:
    if hasattr(action, "fcurves"):
        curves = action.fcurves
    else:
        curves = (
            curve
            for layer in action.layers
            for strip in layer.strips
            if hasattr(strip, "channelbags")
            for channelbag in strip.channelbags
            for curve in channelbag.fcurves
        )
    for curve in curves:
        if curve.data_path == "eval_time":
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"


def _configure_absolute_shape_keys(shell, frame_start, frame_end) -> None:
    keys = shell.data.shape_keys
    keys.use_relative = False
    keys.eval_time = 0.0
    keys.keyframe_insert(data_path="eval_time", frame=frame_start)
    keys.eval_time = float((frame_end - frame_start) * 10)
    keys.keyframe_insert(data_path="eval_time", frame=frame_end)
    action = keys.animation_data.action if keys.animation_data else None
    if action is not None:
        _set_linear_interpolation(action)


def _progress_ui_region(context):
    """Find only the initiating Screen's sidebar region, never another window."""
    window = context.window
    screen = window.screen if window is not None else None
    if screen is None:
        return window, screen, None
    if (
        context.area is not None
        and context.area.type == "VIEW_3D"
        and context.region is not None
        and context.region.type == "UI"
    ):
        return window, screen, context.region
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type == "UI":
                return window, screen, region
    return window, screen, None


def _tag_progress_redraw(window, screen, region) -> None:
    if bpy.app.background or window is None or screen is None or region is None:
        return
    # A workspace switch replaces window.screen. Do not leak progress redraws
    # into the newly displayed Screen.
    if window.screen != screen:
        return
    try:
        region.tag_redraw()
    except ReferenceError:
        pass


def _set_bake_progress(
    settings,
    completed,
    total,
    frame=None,
    *,
    progress_window=None,
    progress_screen=None,
    progress_region=None,
) -> None:
    total = max(1, int(total))
    completed = max(0, min(int(completed), total))
    settings.bake_progress = completed / total
    settings.bake_total_frames = total
    if frame is not None:
        settings.bake_current_frame = int(frame)
        settings.bake_progress_text = tr(
            "Frame {frame} / {end} ({percent:.1f}%)",
            frame=frame,
            end=settings.frame_end,
            percent=settings.bake_progress * 100.0,
        )
    else:
        settings.bake_progress_text = tr("Preparing solver...")
    _tag_progress_redraw(progress_window, progress_screen, progress_region)


def _reset_bake_progress(settings) -> None:
    settings.bake_in_progress = False
    settings.bake_progress = 0.0
    settings.bake_current_frame = settings.frame_start
    settings.bake_total_frames = 0
    settings.bake_progress_text = tr("Not started")


class KOROMO_Settings(PropertyGroup):
    source_mode: EnumProperty(
        name="Garment Source",
        description="Use one mesh object or a HOU clothes collection",
        items=(
            ("OBJECT", "Mesh Object", "Use one SHELL mesh object"),
            ("HOU", "HOU Collection", "Use a verified HOU sewing plan and all of its parts"),
        ),
        default="OBJECT",
    )
    shell_object: PointerProperty(
        name="Source SHELL",
        description="Source garment evaluated at the first bake frame",
        type=bpy.types.Object,
        poll=_mesh_object,
    )
    hou_collection: PointerProperty(
        name="HOU Collection",
        description="Clothes collection carrying housei_sewing_plan_json",
        type=bpy.types.Collection,
    )
    static_object: PointerProperty(
        name="Source BODY",
        description="Source collision body whose animation modifiers are copied and evaluated every frame",
        type=bpy.types.Object,
        poll=_mesh_object,
    )
    static_crop_enabled: BoolProperty(
        name="Crop BODY Collider",
        description="Keep a topology-stable animated collision band between two world-Z planes",
        default=True,
    )
    static_crop_min_z: FloatProperty(
        name="Lower Z",
        description="Lower world-Z cut plane; polygons crossing the plane are retained",
        default=0.40,
        subtype="DISTANCE",
        unit="LENGTH",
    )
    static_crop_max_z: FloatProperty(
        name="Upper Z",
        description="Upper world-Z cut plane; polygons crossing the plane are retained",
        default=1.45,
        subtype="DISTANCE",
        unit="LENGTH",
    )
    prepared_shell_object: PointerProperty(
        name="Prepared SHELL",
        type=bpy.types.Object,
        poll=_mesh_object,
        options={"HIDDEN"},
    )
    prepared_static_object: PointerProperty(
        name="Prepared BODY",
        type=bpy.types.Object,
        poll=_mesh_object,
        options={"HIDDEN"},
    )
    prepared_collection: PointerProperty(
        name="Prepared Collection",
        type=bpy.types.Collection,
        options={"HIDDEN"},
    )
    prepared_source_shell_name: StringProperty(options={"HIDDEN"})
    prepared_source_static_name: StringProperty(options={"HIDDEN"})
    prepared_source_hou_visibility_json: StringProperty(options={"HIDDEN"})
    source_shell_hidden_by_prepare: BoolProperty(default=False, options={"HIDDEN"})
    frame_start: IntProperty(name="Start", default=1, min=-1048574, max=1048574)
    frame_end: IntProperty(name="End", default=250, min=-1048574, max=1048574)
    time_scale: FloatProperty(name="Time Scale", default=1.0, min=0.001, max=100.0)
    gravity: FloatVectorProperty(
        name="Gravity",
        default=(0.0, 0.0, -9.81),
        size=3,
        subtype="XYZ",
    )
    substeps: IntProperty(name="Substeps", default=6, min=1, max=128)
    adaptive_substeps_enabled: BoolProperty(
        name="Adaptive BODY Substeps",
        description="Sample large BODY motion at Blender subframes while keeping Substeps as the normal-frame minimum",
        default=True,
    )
    adaptive_max_substeps: IntProperty(
        name="Maximum Adaptive Substeps",
        description="Upper bound for effective substeps on frames with large BODY motion",
        default=128,
        min=1,
        max=1024,
    )
    pd_iterations: IntProperty(name="PD Iterations", default=10, min=1, max=256)
    pcg_iterations: IntProperty(name="PCG Iterations", default=120, min=1, max=4096)
    pcg_tolerance: FloatProperty(
        name="PCG Tolerance",
        default=1.0e-6,
        min=1.0e-8,
        max=0.1,
        precision=6,
    )
    collision_iterations: IntProperty(
        name="Collision Safety Passes",
        description="Hard post-solve contact projections; zero preserves coupled strain constraints",
        default=0,
        min=0,
        max=64,
    )
    velocity_damping: FloatProperty(
        name="Velocity Damping", default=0.01, min=0.0, max=1.0
    )
    thread_count: IntProperty(
        name="Threads",
        description="Zero uses the OpenMP maximum",
        default=0,
        min=0,
        max=1024,
    )
    density: FloatProperty(name="Density", default=1.0, min=1.0e-6, max=1.0e6)
    stretch_stiffness: FloatProperty(
        name="Stretch", default=5000.0, min=0.0, max=1.0e9
    )
    bend_stiffness: FloatProperty(name="Bend", default=5.0, min=0.0, max=1.0e9)
    strain_limit_enabled: BoolProperty(
        name="Strain Limit",
        description="Couple per-triangle tension and compression bounds into the PD/ADMM system",
        default=True,
    )
    strain_limit_percent: FloatProperty(
        name="Maximum In-Plane Strain",
        description="Maximum absolute projected principal strain; 5% keeps triangle stretches between 0.95 and 1.05",
        default=5.0,
        min=0.1,
        max=100.0,
        subtype="PERCENTAGE",
        precision=2,
    )
    strain_limit_stiffness: FloatProperty(
        name="Limit Solver Weight",
        description="ADMM penalty weight controlling tension and compression convergence",
        default=1000000.0,
        min=1.0,
        max=1.0e9,
    )
    seam_enabled: BoolProperty(
        name="Seam Threads",
        description="Use marked sewing edges, with boundary proximity as a fallback",
        default=True,
    )
    seam_attribute: StringProperty(
        name="Seam Attribute",
        description="Boolean EDGE attribute containing explicit sewing pairs",
        default=_DEFAULT_SEAM_ATTRIBUTE,
    )
    seam_search_distance: FloatProperty(
        name="Fallback Distance",
        description="Maximum distance for boundary pairing when the seam attribute is absent",
        default=0.01,
        min=1.0e-6,
        max=1.0,
        subtype="DISTANCE",
        precision=4,
    )
    seam_stiffness: FloatProperty(
        name="Seam Stiffness",
        description="Finite seam strength solved together with cloth and contact constraints",
        default=1000000.0,
        min=1.0,
        max=1.0e9,
    )
    thickness: FloatProperty(
        name="Thickness", default=0.002, min=0.0, max=1000.0, subtype="DISTANCE"
    )
    friction: FloatProperty(name="Friction", default=0.3, min=0.0, max=1.0)
    restitution: FloatProperty(name="Restitution", default=0.0, min=0.0, max=1.0)
    last_prepare_status: StringProperty(name="Prepare Status", default="Not prepared")
    last_prepare_skipped: IntProperty(name="Skipped BODY Triangles", default=0)
    last_static_crop_vertices: StringProperty(default="-", options={"HIDDEN"})
    last_static_crop_polygons: StringProperty(default="-", options={"HIDDEN"})
    last_seam_count: IntProperty(name="Detected Seams", default=0)
    last_seam_source: StringProperty(default="-", options={"HIDDEN"})
    last_status: StringProperty(name="Status", default="Not baked")
    last_contacts: StringProperty(name="Contacts", default="-")
    last_residual: StringProperty(name="PCG Residual", default="-")
    last_strain: StringProperty(name="Maximum Principal Stretch", default="-")
    last_adaptive_status: StringProperty(default="-", options={"HIDDEN"})
    bake_in_progress: BoolProperty(default=False, options={"HIDDEN"})
    bake_progress: FloatProperty(
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        options={"HIDDEN"},
    )
    bake_current_frame: IntProperty(default=1, options={"HIDDEN"})
    bake_total_frames: IntProperty(default=0, options={"HIDDEN"})
    bake_progress_text: StringProperty(default="Not started", options={"HIDDEN"})


class KOROMO_OT_set_active_shell(Operator):
    bl_idname = "koromo.set_active_shell"
    bl_label = "Use Active as SHELL"
    bl_description = "Assign the active mesh as the deformable SHELL"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        context.scene.koromo_settings.shell_object = context.active_object
        return {"FINISHED"}


class KOROMO_OT_set_active_static(Operator):
    bl_idname = "koromo.set_active_static"
    bl_label = "Use Active as BODY"
    bl_description = "Assign the active mesh as the animated body collider"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        context.scene.koromo_settings.static_object = context.active_object
        return {"FINISHED"}


class KOROMO_OT_prepare(Operator):
    bl_idname = "koromo.prepare"
    bl_label = "Prepare Simulation Copies"
    bl_description = (
        "Create simulation copies and preserve the BODY animation modifier stack"
    )

    def execute(self, context):
        if _BAKE_RUNNING:
            self.report({"ERROR"}, tr("A solver bake is already running"))
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            self.report(
                {"ERROR"}, tr("Switch Blender to Object Mode before preparing")
            )
            return {"CANCELLED"}

        settings = context.scene.koromo_settings
        source_mode = settings.source_mode
        source_shell = settings.shell_object
        source_hou = settings.hou_collection
        source_static = settings.static_object
        if source_static is None or (
            source_mode == "OBJECT" and source_shell is None
        ) or (source_mode == "HOU" and source_hou is None):
            self.report(
                {"ERROR"},
                tr(
                    "Assign a garment source and source BODY mesh"
                ),
            )
            return {"CANCELLED"}
        if source_mode == "OBJECT" and source_shell == source_static:
            self.report(
                {"ERROR"}, tr("Source SHELL and BODY must be different objects")
            )
            return {"CANCELLED"}

        old_frame = context.scene.frame_current
        collection = None
        prepared_shell = None
        prepared_static = None
        hou_plan = None
        try:
            context.scene.frame_set(settings.frame_start)
            _remove_prepared(settings, restore_visibility=True)
            settings.last_prepare_skipped = 0
            settings.last_seam_count = 0
            settings.last_seam_source = "-"
            settings.last_static_crop_vertices = "-"
            settings.last_static_crop_polygons = "-"

            collection = bpy.data.collections.new(_PREPARED_COLLECTION_NAME)
            collection[_PREPARED_COLLECTION_TAG] = _PREPARED_VERSION
            context.scene.collection.children.link(collection)
            depsgraph = context.evaluated_depsgraph_get()

            if source_mode == "HOU":
                hou_plan = read_hou_plan(source_hou)
                if source_static in hou_plan.parts:
                    raise RuntimeError("Source BODY cannot be one of the HOU garment parts")
                prepared_shell = build_combined_shell(
                    hou_plan, f"{hou_plan.collection.name}_KOROMO_SHELL"
                )
                prepared_source_name = hou_plan.collection.name
            else:
                prepared_shell = _evaluated_snapshot(
                    source_shell,
                    depsgraph,
                    f"{source_shell.name}_KOROMO_SHELL",
                )
                prepared_source_name = source_shell.name
            collection.objects.link(prepared_shell)
            prepared_shell[_PREPARED_OBJECT_TAG] = _PREPARED_VERSION
            prepared_shell[_PREPARED_ROLE_TAG] = "SHELL"
            prepared_shell[_PREPARED_SOURCE_TAG] = prepared_source_name
            prepared_shell[_PREPARED_SOURCE_MODE_TAG] = source_mode
            if hou_plan is not None:
                prepared_shell[_PREPARED_HOU_DIGEST_TAG] = hou_plan.digest
            _validate_shell_mesh(prepared_shell.data)
            if settings.seam_enabled:
                if hou_plan is not None:
                    seam_pairs = list(hou_plan.seam_pairs)
                    if not seam_pairs:
                        raise RuntimeError(
                            "HOU sewing plan has no pairs; disable Seam Threads only for an unsewn sheet"
                        )
                    seam_source = tr(
                        "HOU plan {name}", name=hou_plan.collection.name
                    )
                else:
                    seam_pairs, seam_source = _seam_pairs(
                        prepared_shell.data,
                        settings.seam_attribute,
                        settings.seam_search_distance,
                    )
            else:
                seam_pairs, seam_source = [], tr("disabled")
            flattened_seams = [vertex for pair in seam_pairs for vertex in pair]
            if flattened_seams:
                prepared_shell[_PREPARED_SEAMS_TAG] = flattened_seams
            prepared_shell[_PREPARED_SEAM_DISTANCE_TAG] = settings.seam_search_distance
            prepared_shell[_PREPARED_SEAM_ENABLED_TAG] = settings.seam_enabled
            prepared_shell[_PREPARED_SEAM_ATTRIBUTE_TAG] = settings.seam_attribute.strip()
            prepared_shell[_PREPARED_SEAM_SOURCE_TAG] = seam_source

            prepared_static = _animated_static_copy(
                source_static, f"{source_static.name}_KOROMO_BODY"
            )
            collection.objects.link(prepared_static)
            prepared_static.hide_set(False)
            prepared_static[_PREPARED_OBJECT_TAG] = _PREPARED_VERSION
            prepared_static[_PREPARED_ROLE_TAG] = "STATIC"
            prepared_static[_PREPARED_SOURCE_TAG] = source_static.name
            prepared_static[_PREPARED_CROP_ENABLED_TAG] = settings.static_crop_enabled
            prepared_static[_PREPARED_CROP_MIN_TAG] = settings.static_crop_min_z
            prepared_static[_PREPARED_CROP_MAX_TAG] = settings.static_crop_max_z
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()
            if settings.static_crop_enabled:
                kept_vertices, source_vertices, kept_polygons, source_polygons = (
                    _configure_static_crop(
                        prepared_static,
                        depsgraph,
                        settings.static_crop_min_z,
                        settings.static_crop_max_z,
                    )
                )
                settings.last_static_crop_vertices = (
                    f"{kept_vertices:,} / {source_vertices:,}"
                )
                settings.last_static_crop_polygons = (
                    f"{kept_polygons:,} / {source_polygons:,}"
                )
                context.view_layer.update()
                depsgraph = context.evaluated_depsgraph_get()
            (_static_vertices, static_triangles, skipped,
             _static_topology) = _filtered_static_mesh(prepared_static, depsgraph)
            if not static_triangles:
                raise RuntimeError("Prepared BODY has no usable triangles")

            settings.prepared_collection = collection
            settings.prepared_shell_object = prepared_shell
            settings.prepared_static_object = prepared_static
            settings.prepared_source_shell_name = prepared_source_name
            settings.prepared_source_static_name = source_static.name
            settings.last_prepare_skipped = skipped
            settings.last_seam_count = len(seam_pairs)
            settings.last_seam_source = seam_source
            settings.last_prepare_status = tr(
                "Prepared animated BODY in {collection}; {seams} seams from {source}; "
                "{triangles} BODY triangles; skipped {skipped} tiny BODY triangles",
                collection=collection.name,
                seams=len(seam_pairs),
                source=seam_source,
                triangles=f"{len(static_triangles):,}",
                skipped=skipped,
            )
            settings.last_status = tr("Ready to bake")
            settings.last_contacts = "-"
            settings.last_residual = "-"
            settings.last_strain = "-"
            settings.last_adaptive_status = "-"
            _reset_bake_progress(settings)

            if hou_plan is not None:
                _hide_hou_source_parts(settings, hou_plan.parts)
            elif not source_shell.hide_get():
                source_shell.hide_set(True)
                settings.source_shell_hidden_by_prepare = True
            for obj in context.selected_objects:
                obj.select_set(False)
            prepared_shell.select_set(True)
            context.view_layer.objects.active = prepared_shell
            self.report({"INFO"}, settings.last_prepare_status)
            return {"FINISHED"}
        except Exception as exc:
            if collection is not None:
                settings.prepared_shell_object = prepared_shell
                settings.prepared_static_object = prepared_static
                settings.prepared_collection = collection
                _remove_prepared(settings, restore_visibility=True)
            settings.last_prepare_status = tr(
                "Prepare failed: {error}", error=tr(str(exc))
            )
            settings.last_prepare_skipped = 0
            settings.last_seam_count = 0
            settings.last_seam_source = "-"
            settings.last_status = settings.last_prepare_status
            self.report({"ERROR"}, settings.last_prepare_status)
            return {"CANCELLED"}
        finally:
            context.scene.frame_set(old_frame)


class KOROMO_OT_clear_prepared(Operator):
    bl_idname = "koromo.clear_prepared"
    bl_label = "Clear Prepared"
    bl_description = "Remove simulation copies created by Koromo"

    def execute(self, context):
        if _BAKE_RUNNING:
            self.report({"ERROR"}, tr("A solver bake is already running"))
            return {"CANCELLED"}
        removed = _remove_prepared(
            context.scene.koromo_settings,
            restore_visibility=True,
        )
        settings = context.scene.koromo_settings
        settings.last_prepare_skipped = 0
        settings.last_seam_count = 0
        settings.last_seam_source = "-"
        settings.last_prepare_status = tr("Not prepared")
        settings.last_status = (
            tr("Prepared copies cleared") if removed else tr("Nothing to clear")
        )
        settings.last_contacts = "-"
        settings.last_residual = "-"
        settings.last_strain = "-"
        settings.last_adaptive_status = "-"
        _reset_bake_progress(settings)
        self.report({"INFO"}, settings.last_status)
        return {"FINISHED"}


class KOROMO_OT_clear_bake(Operator):
    bl_idname = "koromo.clear_bake"
    bl_label = "Clear Bake"
    bl_description = "Remove Shape Keys created by Koromo"

    def execute(self, context):
        shell = context.scene.koromo_settings.prepared_shell_object
        if not _is_prepared_object(shell):
            self.report({"ERROR"}, tr("Run Prepare Simulation Copies first"))
            return {"CANCELLED"}
        try:
            if not _clear_owned_bake(shell):
                self.report({"INFO"}, tr("SHELL has no solver bake"))
        except RuntimeError as exc:
            self.report({"ERROR"}, tr(str(exc)))
            return {"CANCELLED"}
        settings = context.scene.koromo_settings
        settings.last_status = tr("Bake cleared")
        _reset_bake_progress(settings)
        return {"FINISHED"}


class _BakeJob:
    """State shared by synchronous background and interactive modal bakes."""

    def __init__(self, context, settings, shell, static):
        self.scene = context.scene
        self.settings = settings
        self.shell = shell
        self.static = static
        self.old_frame = self.scene.frame_current
        self.old_subframe = self.scene.frame_subframe
        (
            self.progress_window,
            self.progress_screen,
            self.progress_region,
        ) = _progress_ui_region(context)
        self.total_frames = settings.frame_end - settings.frame_start + 1
        self.next_frame = settings.frame_start + 1
        self.solver = None
        self.cache = None
        self.cache_value_count = 0
        self.buffered_frames = 0
        self.created_bake = False
        self.static_topology = None
        self.previous_static_vertices = None
        self.motion_threshold = 0.0
        self.frame_dt = 0.0
        self.world_to_local = None
        self.seam_pairs = []
        self.final_stats = None
        self.adaptive_frames = 0
        self.adaptive_capped_frames = 0
        self.peak_effective_substeps = int(settings.substeps)
        self.maximum_body_motion = 0.0

    def _set_progress(self, completed, frame=None):
        _set_bake_progress(
            self.settings,
            completed,
            self.total_frames,
            frame,
            progress_window=self.progress_window,
            progress_screen=self.progress_screen,
            progress_region=self.progress_region,
        )

    def _restore_display_frame(self, *, force=False):
        if force or not bpy.app.background:
            self.scene.frame_set(self.old_frame, subframe=self.old_subframe)

    def prepare(self, context):
        self._set_progress(0)
        self.scene.frame_set(self.settings.frame_start)
        if _owned_bake(self.shell):
            _clear_owned_bake(self.shell)

        shell_vertices, shell_triangles = _shell_mesh(self.shell)
        depsgraph = context.evaluated_depsgraph_get()
        (
            static_vertices,
            static_triangles,
            skipped_static,
            self.static_topology,
        ) = _filtered_static_mesh(self.static, depsgraph)
        self.seam_pairs = (
            _prepared_seam_pairs(self.shell) if self.settings.seam_enabled else []
        )
        if not shell_triangles:
            raise RuntimeError("SHELL has no triangles")
        if not static_triangles:
            raise RuntimeError("STATIC has no triangles")
        self.settings.last_prepare_skipped = skipped_static

        library = get_library()
        desc = library.default_desc()
        desc.gravity = Vec3(*self.settings.gravity)
        desc.substeps = self.settings.substeps
        desc.pd_iterations = self.settings.pd_iterations
        desc.pcg_iterations = self.settings.pcg_iterations
        desc.pcg_relative_tolerance = self.settings.pcg_tolerance
        desc.collision_iterations = self.settings.collision_iterations
        desc.velocity_damping = self.settings.velocity_damping
        desc.thread_count = self.settings.thread_count

        material = library.default_material()
        material.density = self.settings.density
        material.stretch_stiffness = self.settings.stretch_stiffness
        material.bend_stiffness = self.settings.bend_stiffness
        material.thickness = self.settings.thickness
        material.friction = self.settings.friction
        material.restitution = self.settings.restitution
        material.strain_limit = (
            self.settings.strain_limit_percent * 0.01
            if self.settings.strain_limit_enabled
            else 0.0
        )
        material.strain_limit_stiffness = self.settings.strain_limit_stiffness

        fps = self.scene.render.fps / self.scene.render.fps_base
        self.frame_dt = self.settings.time_scale / fps
        self.world_to_local = self.shell.matrix_world.inverted_safe()
        self.motion_threshold = _adaptive_motion_threshold(
            shell_vertices, shell_triangles
        )
        self.previous_static_vertices = static_vertices
        self.cache_value_count = len(shell_vertices) * 3
        self.cache = tempfile.TemporaryFile(mode="w+b")

        self.solver = library.create(desc)
        self.solver.set_static_mesh(static_vertices, static_triangles)
        self.solver.set_shell_mesh(shell_vertices, shell_triangles, material)
        self.solver.set_shell_seams(
            self.seam_pairs, self.settings.seam_stiffness
        )
        self.solver.build()
        self._restore_display_frame()
        self._set_progress(1, self.settings.frame_start)

    def _evaluated_body(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        vertices, topology = _evaluated_static_vertices(self.static, depsgraph)
        if topology != self.static_topology:
            raise RuntimeError(
                "Animated STATIC topology changed; use deformation-only modifiers"
            )
        return vertices

    def process_next_frame(self, context) -> bool:
        frame = self.next_frame
        if frame > self.settings.frame_end:
            return False

        self.scene.frame_set(frame)
        endpoint_vertices = self._evaluated_body(context)
        motion = _maximum_vertex_motion(
            self.previous_static_vertices, endpoint_vertices
        )
        self.maximum_body_motion = max(self.maximum_body_motion, motion)
        if self.settings.adaptive_substeps_enabled:
            calls, capped = _adaptive_step_calls(
                motion,
                self.motion_threshold,
                self.settings.substeps,
                self.settings.adaptive_max_substeps,
            )
        else:
            calls, capped = 1, False
        if calls > 1:
            self.adaptive_frames += 1
        if capped:
            self.adaptive_capped_frames += 1
        self.peak_effective_substeps = max(
            self.peak_effective_substeps,
            calls * int(self.settings.substeps),
        )

        if calls > 1:
            # The endpoint was evaluated first to choose the call count. Reset
            # the depsgraph to the previous integer frame before sampling the
            # interval in strictly increasing time order.
            self.scene.frame_set(frame - 1)
        for sample in range(1, calls + 1):
            if sample == calls:
                target_vertices = endpoint_vertices
            else:
                self.scene.frame_set(frame - 1, subframe=sample / calls)
                target_vertices = self._evaluated_body(context)
            self.solver.update_static_vertices(target_vertices)
            self.solver.step(self.frame_dt / calls)
            self.final_stats = self.solver.stats()

        flattened = array(
            "f",
            _world_to_local_flat(self.world_to_local, self.solver.positions()),
        )
        flattened.tofile(self.cache)
        self.buffered_frames += 1
        self.previous_static_vertices = endpoint_vertices
        self.next_frame += 1

        # Never expose the evaluation frame or subframe to the visible Screen.
        self._restore_display_frame()
        completed = frame - self.settings.frame_start + 1
        self._set_progress(completed, frame)
        return self.next_frame <= self.settings.frame_end

    def materialize_bake(self):
        if self.buffered_frames != self.total_frames - 1:
            raise RuntimeError("Simulation result buffer is incomplete")
        self._restore_display_frame(force=True)
        basis = self.shell.shape_key_add(name="Basis", from_mix=False)
        basis.interpolation = "KEY_LINEAR"
        keys = self.shell.data.shape_keys
        keys[_BAKE_TAG] = 1
        keys["frame_start"] = self.settings.frame_start
        keys["frame_end"] = self.settings.frame_end
        self.created_bake = True

        self.cache.seek(0)
        for frame in range(
            self.settings.frame_start + 1, self.settings.frame_end + 1
        ):
            coordinates = array("f")
            coordinates.fromfile(self.cache, self.cache_value_count)
            shape = self.shell.shape_key_add(
                name=f"KOROMO_{frame:06d}", from_mix=False
            )
            shape.interpolation = "KEY_LINEAR"
            shape.data.foreach_set("co", coordinates)
        _configure_absolute_shape_keys(
            self.shell, self.settings.frame_start, self.settings.frame_end
        )
        self.shell.active_shape_key_index = 0
        self.shell.show_only_shape_key = False
        self._restore_display_frame(force=True)

    def finish_success(self):
        self.materialize_bake()
        self.settings.last_status = tr(
            "Baked {count} frames with {seams} seams and animated BODY; "
            "cursor restored to frame {frame}",
            count=self.total_frames,
            seams=len(self.seam_pairs),
            frame=self.scene.frame_current,
        )
        self.settings.bake_progress = 1.0
        self.settings.bake_current_frame = self.settings.frame_end
        self.settings.bake_progress_text = tr(
            "Completed: {count} frames", count=self.total_frames
        )
        if self.settings.adaptive_substeps_enabled:
            self.settings.last_adaptive_status = tr(
                "{frames} frames; peak {substeps} substeps; max BODY motion {motion:.4g} m; capped {capped}",
                frames=self.adaptive_frames,
                substeps=self.peak_effective_substeps,
                motion=self.maximum_body_motion,
                capped=self.adaptive_capped_frames,
            )
        else:
            self.settings.last_adaptive_status = tr("disabled")
        if self.final_stats is not None:
            self.settings.last_contacts = str(self.final_stats.contact_count)
            self.settings.last_residual = (
                f"{self.final_stats.final_pcg_relative_residual:.3g}"
            )
            self.settings.last_strain = tr(
                "{percent:.2f}% ({count} projections)",
                percent=(self.final_stats.maximum_principal_stretch - 1.0) * 100.0,
                count=self.final_stats.strain_limit_projection_count,
            )
        self.close()
        _tag_progress_redraw(
            self.progress_window, self.progress_screen, self.progress_region
        )

    def finish_failure(self, error=None, *, cancelled=False):
        if self.created_bake and _owned_bake(self.shell):
            _clear_owned_bake(self.shell)
        if cancelled:
            self.settings.last_status = tr("Bake cancelled")
            self.settings.bake_progress_text = tr("Bake cancelled")
        else:
            self.settings.last_status = tr(
                "Bake failed: {error}", error=tr(str(error))
            )
            self.settings.bake_progress_text = tr(
                "Stopped at frame {frame}", frame=self.settings.bake_current_frame
            )
        self.close()
        _tag_progress_redraw(
            self.progress_window, self.progress_screen, self.progress_region
        )

    def close(self):
        if self.solver is not None:
            self.solver.close()
            self.solver = None
        if self.cache is not None:
            self.cache.close()
            self.cache = None
        self._restore_display_frame(force=True)


class KOROMO_OT_bake(Operator):
    bl_idname = "koromo.bake"
    bl_label = "Bake Simulation"
    bl_description = "Run the OpenMP solver and bake absolute Shape Keys"

    _job = None
    _timer = None

    def _release(self, context):
        global _BAKE_RUNNING
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        settings = (
            self._job.settings
            if self._job is not None
            else context.scene.koromo_settings
        )
        settings.bake_in_progress = False
        _BAKE_RUNNING = False
        self._job = None

    def _fail(self, context, error, *, cancelled=False):
        if self._job is not None:
            self._job.finish_failure(error, cancelled=cancelled)
            message = self._job.settings.last_status
        else:
            message = tr("Bake failed: {error}", error=tr(str(error)))
        self.report({"WARNING" if cancelled else "ERROR"}, message)
        self._release(context)
        return {"CANCELLED"}

    def _run_synchronous(self, context):
        try:
            while self._job.process_next_frame(context):
                pass
            self._job.finish_success()
            message = self._job.settings.last_status
            self.report({"INFO"}, message)
            self._release(context)
            return {"FINISHED"}
        except Exception as exc:
            return self._fail(context, exc)

    def execute(self, context):
        global _BAKE_RUNNING
        if _BAKE_RUNNING:
            self.report({"ERROR"}, tr("A solver bake is already running"))
            return {"CANCELLED"}

        settings = context.scene.koromo_settings
        if context.mode != "OBJECT":
            self.report({"ERROR"}, tr("Switch Blender to Object Mode before baking"))
            return {"CANCELLED"}
        try:
            shell, static = _prepared_pair(settings)
        except RuntimeError as exc:
            self.report({"ERROR"}, tr(str(exc)))
            return {"CANCELLED"}
        if settings.frame_end <= settings.frame_start:
            self.report({"ERROR"}, tr("End frame must be greater than start frame"))
            return {"CANCELLED"}
        if shell.data.shape_keys and not _owned_bake(shell):
            self.report(
                {"ERROR"}, tr("Prepared SHELL was modified; run Prepare again")
            )
            return {"CANCELLED"}
        if abs(shell.matrix_world.determinant()) < 1.0e-12:
            self.report({"ERROR"}, tr("SHELL object transform is singular"))
            return {"CANCELLED"}

        _BAKE_RUNNING = True
        settings.bake_in_progress = True
        settings.bake_progress = 0.0
        settings.bake_current_frame = settings.frame_start
        settings.bake_total_frames = settings.frame_end - settings.frame_start + 1
        settings.bake_progress_text = tr("Preparing solver...")
        self._job = _BakeJob(context, settings, shell, static)
        try:
            self._job.prepare(context)
        except Exception as exc:
            return self._fail(context, exc)

        if bpy.app.background:
            return self._run_synchronous(context)

        self._timer = context.window_manager.event_timer_add(
            0.01, window=context.window
        )
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            return self._fail(context, None, cancelled=True)
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        try:
            if self._job.process_next_frame(context):
                return {"RUNNING_MODAL"}
            self._job.finish_success()
            message = self._job.settings.last_status
            self.report({"INFO"}, message)
            self._release(context)
            return {"FINISHED"}
        except Exception as exc:
            return self._fail(context, exc)

    def cancel(self, context):
        if self._job is not None:
            self._job.finish_failure(None, cancelled=True)
        self._release(context)


class KOROMO_PT_solver(Panel):
    bl_label = "Koromo"
    bl_idname = "KOROMO_PT_solver"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Koromo"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        settings = context.scene.koromo_settings

        try:
            library = get_library()
            icon = "CHECKMARK" if library.openmp_enabled else "ERROR"
            text = tr(
                "DLL ready - OpenMP"
                if library.openmp_enabled
                else "DLL has no OpenMP"
            )
        except NativeSolverError as exc:
            icon = "ERROR"
            text = tr(str(exc))
        layout.label(text=text, icon=icon)

        objects = layout.box()
        objects.label(text="Garment Source")
        objects.prop(settings, "source_mode", expand=True)
        if settings.source_mode == "HOU":
            objects.prop(settings, "hou_collection")
        else:
            objects.prop(settings, "shell_object")
            objects.operator("koromo.set_active_shell", icon="OUTLINER_OB_MESH")
        objects.prop(settings, "static_object")
        objects.operator("koromo.set_active_static", icon="MOD_PHYSICS")
        objects.prop(settings, "static_crop_enabled")
        crop = objects.column(align=True)
        crop.enabled = settings.static_crop_enabled
        crop.prop(settings, "static_crop_min_z")
        crop.prop(settings, "static_crop_max_z")

        prepare_row = objects.row(align=True)
        prepare_row.scale_y = 1.25
        prepare_row.operator("koromo.prepare", icon="DUPLICATE")
        prepare_row.operator("koromo.clear_prepared", icon="X")

        prepared = layout.box()
        prepared.label(text="Simulation Copies")
        shell_name = (
            settings.prepared_shell_object.name
            if settings.prepared_shell_object is not None
            else "-"
        )
        static_name = (
            settings.prepared_static_object.name
            if settings.prepared_static_object is not None
            else "-"
        )
        prepared.label(text=tr("SHELL: {name}", name=shell_name), icon="MESH_GRID")
        prepared.label(text=tr("BODY: {name}", name=static_name), icon="MOD_PHYSICS")
        static_modifier_count = (
            len(settings.prepared_static_object.modifiers)
            if settings.prepared_static_object is not None
            else 0
        )
        prepared.label(
            text=tr("Live BODY modifiers: {count}", count=static_modifier_count),
            icon="MODIFIER",
        )
        if settings.static_crop_enabled:
            prepared.label(
                text=tr(
                    "Crop vertices: {value}",
                    value=settings.last_static_crop_vertices,
                ),
                icon="VERTEXSEL",
            )
            prepared.label(
                text=tr(
                    "Crop polygons: {value}",
                    value=settings.last_static_crop_polygons,
                ),
                icon="FACESEL",
            )
        prepared.label(text=tr(settings.last_prepare_status), icon="INFO")

        timing = layout.box()
        timing.label(text="Bake Range")
        row = timing.row(align=True)
        row.prop(settings, "frame_start")
        row.prop(settings, "frame_end")
        timing.prop(settings, "time_scale")

        bake_progress = layout.box()
        bake_progress.label(text="Bake Progress")
        bake_progress.progress(
            factor=settings.bake_progress,
            type="BAR",
            text=tr(settings.bake_progress_text),
            translate=False,
        )

        material = layout.box()
        material.label(text="SHELL Material")
        material.prop(settings, "density")
        material.prop(settings, "stretch_stiffness")
        material.prop(settings, "bend_stiffness")
        material.prop(settings, "strain_limit_enabled")
        strain = material.column()
        strain.enabled = settings.strain_limit_enabled
        strain.prop(settings, "strain_limit_percent")
        strain.prop(settings, "strain_limit_stiffness")
        material.prop(settings, "thickness")
        material.prop(settings, "friction")
        material.prop(settings, "restitution")

        seams = layout.box()
        seams.label(text="Seam Threads")
        seams.prop(settings, "seam_enabled")
        seam_settings = seams.column()
        seam_settings.enabled = settings.seam_enabled
        if settings.source_mode == "HOU":
            seam_settings.label(text="Exact pairs from HOU sewing plan")
        else:
            seam_settings.prop(settings, "seam_attribute")
            seam_settings.prop(settings, "seam_search_distance")
        seam_settings.prop(settings, "seam_stiffness")
        seams.label(
            text=tr("Detected pairs: {count}", count=settings.last_seam_count)
        )
        seams.label(text=tr("Source: {value}", value=settings.last_seam_source))

        solver = layout.box()
        solver.label(text="Solver")
        solver.prop(settings, "gravity")
        solver.prop(settings, "substeps")
        solver.prop(settings, "adaptive_substeps_enabled")
        adaptive = solver.column()
        adaptive.enabled = settings.adaptive_substeps_enabled
        adaptive.prop(settings, "adaptive_max_substeps")
        solver.prop(settings, "pd_iterations")
        solver.prop(settings, "pcg_iterations")
        solver.prop(settings, "pcg_tolerance")
        solver.prop(settings, "collision_iterations")
        solver.prop(settings, "velocity_damping")
        solver.prop(settings, "thread_count")

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.enabled = not settings.bake_in_progress
        row.operator("koromo.bake", icon="PHYSICS")
        row.operator("koromo.clear_bake", icon="TRASH")

        status = layout.box()
        status.label(text=tr(settings.last_status), icon="INFO")
        status.label(text=tr("Last contacts: {value}", value=settings.last_contacts))
        status.label(
            text=tr("Last PCG residual: {value}", value=settings.last_residual)
        )
        status.label(text=tr("Last max strain: {value}", value=settings.last_strain))
        status.label(
            text=tr("Last adaptive sampling: {value}", value=settings.last_adaptive_status)
        )


_CLASSES = (
    KOROMO_Settings,
    KOROMO_OT_set_active_shell,
    KOROMO_OT_set_active_static,
    KOROMO_OT_prepare,
    KOROMO_OT_clear_prepared,
    KOROMO_OT_clear_bake,
    KOROMO_OT_bake,
    KOROMO_PT_solver,
)


def register():
    bpy.app.translations.register(__package__, translations_dict)
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.koromo_settings = PointerProperty(type=KOROMO_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "koromo_settings"):
        del bpy.types.Scene.koromo_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.app.translations.unregister(__package__)
