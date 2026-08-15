"""Read a HOU/Housei clothes collection without importing Housei code."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import bpy

from .i18n import tr


PLAN_PROPERTY = "housei_sewing_plan_json"
PLAN_SCHEMA_PREFIX = "housei-sewing-plan/1."


class HouReadError(RuntimeError):
    """The selected collection does not satisfy the downstream contract."""


@dataclass(frozen=True)
class HouPlan:
    collection: bpy.types.Collection
    raw: str
    digest: str
    payload: dict
    parts: tuple[bpy.types.Object, ...]
    seam_pairs: tuple[tuple[int, int], ...]
    seam_labels: tuple[str, ...]


def _as_int(value, description: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HouReadError(
            tr("HOU sewing plan has invalid {description}", description=description)
        ) from exc
    return result


def read_hou_plan(collection: bpy.types.Collection | None) -> HouPlan:
    if collection is None:
        raise HouReadError(tr("Assign a HOU clothes collection"))
    if str(collection.get("housei_role", "")) != "clothes":
        raise HouReadError(tr(
            "Collection {name} is not a HOU clothes collection", name=collection.name
        ))
    raw = collection.get(PLAN_PROPERTY)
    if not isinstance(raw, str) or not raw.strip():
        raise HouReadError(tr(
            "Collection {name} has no HOU sewing plan", name=collection.name
        ))
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HouReadError(tr(
            "Collection {name} has invalid HOU sewing JSON", name=collection.name
        )) from exc
    if not isinstance(payload, dict):
        raise HouReadError(tr("HOU sewing plan must be a JSON object"))
    schema = str(payload.get("schema", ""))
    if not schema.startswith(PLAN_SCHEMA_PREFIX):
        raise HouReadError(tr(
            "Unsupported HOU sewing plan schema: {schema}",
            schema=schema or "(missing)",
        ))

    entries = payload.get("parts")
    if not isinstance(entries, list) or not entries:
        raise HouReadError(tr("HOU sewing plan has no parts"))
    objects: list[bpy.types.Object] = []
    starts: list[int] = []
    offset = 0
    for slot, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HouReadError(tr("HOU part {name} is invalid", name=slot))
        name = str(entry.get("object", ""))
        obj = collection.objects.get(name) if name else None
        if obj is None or obj.type != "MESH":
            raise HouReadError(tr("HOU part {name} was not found", name=name or slot))
        expected_vertices = _as_int(entry.get("vertices"), f"part {name} vertex count")
        expected_cut = _as_int(entry.get("cut_scheme"), f"part {name} cut scheme")
        try:
            expected_spacing = float(entry.get("mesh_spacing_m"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise HouReadError(tr(
                "HOU part {name} has invalid mesh spacing", name=name
            )) from exc
        stale = (
            len(obj.data.vertices) != expected_vertices
            or int(obj.get("housei_cut_scheme", 0) or 0) != expected_cut
            or not math.isclose(
                float(obj.get("housei_mesh_spacing_m", 0.0)),
                expected_spacing,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
        )
        if stale:
            raise HouReadError(tr(
                "HOU part {name} no longer matches its sewing plan; regenerate HOU",
                name=name,
            ))
        if not isinstance(obj.get("HOU"), str) or not str(obj.get("HOU")).strip():
            raise HouReadError(tr("HOU part {name} has no HOU metadata", name=name))
        attribute = obj.data.attributes.get("housei_pattern_position")
        if (
            attribute is None
            or attribute.domain != "POINT"
            or attribute.data_type != "FLOAT_VECTOR"
            or len(attribute.data) != len(obj.data.vertices)
        ):
            raise HouReadError(tr(
                "HOU part {name} has no valid pattern coordinates", name=name
            ))
        starts.append(offset)
        offset += len(obj.data.vertices)
        objects.append(obj)

    pair_map = payload.get("pairs")
    if not isinstance(pair_map, dict):
        raise HouReadError(tr("HOU sewing plan pairs must be an object"))
    seam_pairs: list[tuple[int, int]] = []
    seam_labels: list[str] = []
    for label, pairs in pair_map.items():
        if not isinstance(pairs, list):
            raise HouReadError(tr("HOU sewing label {label} is invalid", label=label))
        for pair_index, pair in enumerate(pairs):
            if not isinstance(pair, (list, tuple)) or len(pair) != 4:
                raise HouReadError(tr(
                    "HOU sewing pair {label}[{index}] is invalid",
                    label=label,
                    index=pair_index,
                ))
            slot_a, vertex_a, slot_b, vertex_b = (
                _as_int(value, f"pair {label}[{pair_index}]") for value in pair
            )
            if not (0 <= slot_a < len(objects) and 0 <= slot_b < len(objects)):
                raise HouReadError(tr(
                    "HOU sewing pair {label}[{index}] has an invalid part",
                    label=label,
                    index=pair_index,
                ))
            if not (
                0 <= vertex_a < len(objects[slot_a].data.vertices)
                and 0 <= vertex_b < len(objects[slot_b].data.vertices)
            ):
                raise HouReadError(tr(
                    "HOU sewing pair {label}[{index}] has an invalid vertex",
                    label=label,
                    index=pair_index,
                ))
            seam_pairs.append((starts[slot_a] + vertex_a, starts[slot_b] + vertex_b))
            seam_labels.append(str(label))

    expected_pairs = _as_int(payload.get("pair_count", len(seam_pairs)), "pair count")
    if expected_pairs != len(seam_pairs):
        raise HouReadError(tr(
            "HOU sewing plan pair count is stale ({expected} != {actual})",
            expected=expected_pairs,
            actual=len(seam_pairs),
        ))
    return HouPlan(
        collection=collection,
        raw=raw,
        digest=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        payload=payload,
        parts=tuple(objects),
        seam_pairs=tuple(seam_pairs),
        seam_labels=tuple(seam_labels),
    )


def build_combined_shell(plan: HouPlan, name: str) -> bpy.types.Object:
    """Create a world-space triangulated snapshot in plan part order."""
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    triangle_uvs: list[tuple[tuple[float, float], ...] | None] = []
    material_indices: list[int] = []
    materials: list[bpy.types.Material] = []
    material_slots: dict[bpy.types.Material, int] = {}
    pattern_vectors: list[float] = []
    offset = 0

    for obj in plan.parts:
        mesh = obj.data
        local_vertices = [tuple(obj.matrix_world @ vertex.co) for vertex in mesh.vertices]
        vertices.extend(local_vertices)
        pattern = mesh.attributes["housei_pattern_position"]
        for item in pattern.data:
            pattern_vectors.extend(tuple(item.vector))

        local_materials = []
        for material in mesh.materials:
            if material is None:
                local_materials.append(-1)
                continue
            index = material_slots.get(material)
            if index is None:
                index = len(materials)
                material_slots[material] = index
                materials.append(material)
            local_materials.append(index)

        mesh.calc_loop_triangles()
        uv_layer = mesh.uv_layers.active
        flipped = obj.matrix_world.to_3x3().determinant() < 0.0
        for triangle in mesh.loop_triangles:
            indices = [int(index) for index in triangle.vertices]
            loop_indices = [int(index) for index in triangle.loops]
            if flipped:
                indices[1], indices[2] = indices[2], indices[1]
                loop_indices[1], loop_indices[2] = loop_indices[2], loop_indices[1]
            faces.append(tuple(offset + index for index in indices))
            if uv_layer is None:
                triangle_uvs.append(None)
            else:
                triangle_uvs.append(
                    tuple(tuple(uv_layer.data[index].uv) for index in loop_indices)
                )
            source_material = int(triangle.material_index)
            material_indices.append(
                local_materials[source_material]
                if 0 <= source_material < len(local_materials)
                else 0
            )
        offset += len(local_vertices)

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    try:
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        for material in materials:
            mesh.materials.append(material)
        for polygon, material_index in zip(mesh.polygons, material_indices):
            if material_index >= 0:
                polygon.material_index = material_index
        if any(item is not None for item in triangle_uvs):
            uv = mesh.uv_layers.new(name="UVMap")
            for polygon, values in zip(mesh.polygons, triangle_uvs):
                if values is None:
                    continue
                for loop_index, value in zip(polygon.loop_indices, values):
                    uv.data[loop_index].uv = value
        attribute = mesh.attributes.new(
            "housei_pattern_position", "FLOAT_VECTOR", "POINT"
        )
        attribute.data.foreach_set("vector", pattern_vectors)
        obj = bpy.data.objects.new(name, mesh)
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    obj["koromo_hou_collection"] = plan.collection.name
    obj["koromo_hou_plan_digest"] = plan.digest
    obj["koromo_hou_part_count"] = len(plan.parts)
    return obj
