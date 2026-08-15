"""Non-destructive Blender MCP visualization for the continuum cloth DLL.

Executed inside Blender. It creates/replaces only the KOROMO_DLL_DEMO collection.
"""
from __future__ import annotations

import bpy
import importlib.util
import math
from pathlib import Path
from mathutils import Vector


ROOT = Path(r"C:\Users\azoo\git\koromo-cloth-solver")
DLL = ROOT / "build" / "koromo_cloth_solver.dll"
BRIDGE = ROOT / "build" / "blender_bridge" / "native.py"
COLLECTION_NAME = "KOROMO_DLL_DEMO"


def load_bridge():
    spec = importlib.util.spec_from_file_location("koromo_blender_native", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def material(name, color, metallic=0.0, roughness=0.45):
    value = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    value.diffuse_color = color
    value.metallic = metallic
    value.roughness = roughness
    value.use_nodes = True
    principled = next((node for node in value.node_tree.nodes
                       if node.type == "BSDF_PRINCIPLED"), None)
    if principled:
        # Socket indices are stable across localized Blender builds, while
        # display names are not.
        principled.inputs[0].default_value = color
        principled.inputs[1].default_value = metallic
        principled.inputs[2].default_value = roughness
    return value


def mesh_object(collection, name, vertices, triangles, mat):
    mesh = bpy.data.meshes.new(name + "_Mesh")
    mesh.from_pydata(vertices, [], triangles)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def reset_collection(scene):
    old = bpy.data.collections.get(COLLECTION_NAME)
    if old:
        for obj in list(old.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(old)
    collection = bpy.data.collections.new(COLLECTION_NAME)
    scene.collection.children.link(collection)
    return collection


def append_ellipsoid(vertices, triangles, center, radius, segments=32, rings=16):
    base = len(vertices)
    cx, cy, cz = center
    rx, ry, rz = radius
    vertices.append((cx, cy, cz - rz))
    for j in range(1, rings):
        phi = -0.5 * math.pi + math.pi * j / rings
        cp, sp = math.cos(phi), math.sin(phi)
        for i in range(segments):
            theta = 2.0 * math.pi * i / segments
            vertices.append((cx + rx * cp * math.cos(theta),
                             cy + ry * cp * math.sin(theta), cz + rz * sp))
    top = len(vertices)
    vertices.append((cx, cy, cz + rz))
    first = base + 1
    for i in range(segments):
        ni = (i + 1) % segments
        triangles.append((base, first + ni, first + i))
    for j in range(rings - 2):
        row = first + j * segments
        nxt = row + segments
        for i in range(segments):
            ni = (i + 1) % segments
            triangles.extend(((row + i, row + ni, nxt + ni),
                              (row + i, nxt + ni, nxt + i)))
    last = first + (rings - 2) * segments
    for i in range(segments):
        ni = (i + 1) % segments
        triangles.append((last + i, last + ni, top))


def make_collider(collection):
    verts, tris = [], []
    append_ellipsoid(verts, tris, (0.0, 0.0, 0.72), (0.64, 0.50, 0.92),
                     segments=40, rings=24)
    obj = mesh_object(collection, "KOROMO_Body_Collider", verts, tris,
                      material("KOROMO_Body_Blue", (0.035, 0.18, 0.46, 1.0), 0.15, 0.28))
    for poly in obj.data.polygons:
        poly.use_smooth = True
    obj["koromo_role"] = "STATIC_BODY_COLLIDER"
    obj["koromo_topology_stable"] = True
    obj["koromo_source_space"] = "WORLD"
    return obj, verts, tris


def make_square_cloth(n=33, size=1.62, z=1.82):
    verts = []
    for iy in range(n):
        y = -0.5 * size + size * iy / (n - 1)
        for ix in range(n):
            x = -0.5 * size + size * ix / (n - 1)
            verts.append((x, y, z))
    tris = []
    for iy in range(n - 1):
        for ix in range(n - 1):
            a = iy * n + ix
            b = a + 1
            c = a + n
            d = c + 1
            tris.extend(((a, b, d), (a, d, c)))
    return verts, tris


def setup_presentation(scene, collection):
    ground = mesh_object(
        collection, "KOROMO_Ground",
        [(-4, -4, -0.205), (4, -4, -0.205), (4, 4, -0.205), (-4, 4, -0.205)],
        [(0, 1, 2), (0, 2, 3)],
        material("KOROMO_Ground_Mat", (0.018, 0.025, 0.045, 1.0), 0.0, 0.78))
    ground["koromo_role"] = "PRESENTATION_ONLY"

    target = Vector((0.0, 0.0, 0.82))
    camera_data = bpy.data.cameras.new("KOROMO_Demo_Camera_Data")
    camera = bpy.data.objects.new("KOROMO_Demo_Camera", camera_data)
    collection.objects.link(camera)
    camera.location = (3.0, -3.2, 2.65)
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.lens = 58
    scene.camera = camera

    for name, location, energy, size, color in (
            ("KOROMO_Key_Light", (2.2, -2.0, 3.7), 250, 3.0, (1.0, 0.78, 0.64)),
            ("KOROMO_Fill_Light", (-2.5, 1.2, 2.4), 120, 2.5, (0.48, 0.68, 1.0))):
        light_data = bpy.data.lights.new(name + "_Data", "AREA")
        light_data.energy = energy
        light_data.shape = "DISK"
        light_data.size = size
        light_data.color = color
        light = bpy.data.objects.new(name, light_data)
        collection.objects.link(light)
        light.location = location
        light.rotation_euler = (target - light.location).to_track_quat("-Z", "Y").to_euler()

    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 768
    scene.render.resolution_y = 768
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(ROOT / "build" / "koromo_demo_final.png")
    scene.render.film_transparent = False
    return scene.render.filepath


def main():
    native = load_bridge()
    scene = bpy.data.scenes.get("KOROMO Solver Demo") or bpy.data.scenes.new("KOROMO Solver Demo")
    if bpy.context.window:
        bpy.context.window.scene = scene
    scene.world = scene.world or bpy.data.worlds.new("KOROMO Demo World")
    scene.world.color = (0.018, 0.022, 0.032)
    collection = reset_collection(scene)
    body_obj, body_verts, body_tris = make_collider(collection)
    cloth_verts, cloth_tris = make_square_cloth()

    input_obj = mesh_object(
        collection, "KOROMO_Cloth_Input_Wire", cloth_verts, cloth_tris,
        material("KOROMO_Input_Gray", (0.18, 0.18, 0.18, 1.0)))
    input_obj.display_type = "WIRE"
    input_obj.hide_render = True
    input_obj.hide_viewport = True
    input_obj["koromo_role"] = "SHELL_INPUT_REFERENCE"

    with native.ClothSolver(DLL, substeps=6, pd_iterations=6,
                            pcg_iterations=60) as solver:
        cloth_material = solver.default_material()
        cloth_material.density = 0.35
        cloth_material.stretch_stiffness = 3500.0
        cloth_material.bend_stiffness = 1.0
        cloth_material.thickness = 0.014
        cloth_material.friction = 0.72
        cloth_material.strain_limit = 0.10
        cloth_material.strain_limit_stiffness = 100000.0
        solver.set_body(body_verts, body_tris)
        solver.set_cloth(cloth_verts, cloth_tris, cloth_material)
        solver.build()
        total_contacts = 0
        final_stats = None
        output = cloth_verts
        for _frame in range(42):
            # Demonstrates the animated-collider ABI too, while keeping the
            # topology fixed. This demo's vertices happen to remain stationary.
            solver.update_body(body_verts)
            output = solver.step(1.0 / 60.0)
            final_stats = solver.stats()
            total_contacts += int(final_stats.contact_count)

    result_obj = mesh_object(
        collection, "KOROMO_Cloth_Result", output, cloth_tris,
        material("KOROMO_Result_Magenta", (0.72, 0.018, 0.09, 1.0), 0.05, 0.30))
    for poly in result_obj.data.polygons:
        poly.use_smooth = True
    solidify = result_obj.modifiers.new("Display Thickness", "SOLIDIFY")
    solidify.thickness = 0.008
    subdiv = result_obj.modifiers.new("Display Subdivision", "SUBSURF")
    subdiv.levels = 1
    subdiv.render_levels = 1
    result_obj["koromo_role"] = "SHELL_RESULT"
    result_obj["koromo_solver_dll"] = str(DLL)
    result_obj["koromo_algorithm"] = "PD_ADMM_CONTINUUM_BASELINE_NOT_NESTED_DRS"
    result_obj["koromo_frames"] = 42
    result_obj["koromo_total_contacts"] = total_contacts
    result_obj["koromo_final_contacts"] = int(final_stats.contact_count)
    result_obj["koromo_max_principal_stretch"] = float(final_stats.maximum_principal_stretch)
    render_path = setup_presentation(scene, collection)
    bpy.ops.render.render(write_still=True)

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    body_obj.select_set(True)
    result_obj.select_set(True)
    bpy.context.view_layer.objects.active = result_obj

    displacement = max(
        ((output[i][0] - cloth_verts[i][0]) ** 2 +
         (output[i][1] - cloth_verts[i][1]) ** 2 +
         (output[i][2] - cloth_verts[i][2]) ** 2) ** 0.5
        for i in range(len(output)))
    return {
        "collection": collection.name,
        "body_vertices": len(body_verts),
        "body_triangles": len(body_tris),
        "cloth_vertices": len(cloth_verts),
        "cloth_triangles": len(cloth_tris),
        "frames": 42,
        "total_contacts": total_contacts,
        "final_contacts": int(final_stats.contact_count),
        "maximum_displacement": displacement,
        "maximum_principal_stretch": float(final_stats.maximum_principal_stretch),
        "render": render_path,
        "selected": [obj.name for obj in bpy.context.selected_objects],
    }


result = main()
