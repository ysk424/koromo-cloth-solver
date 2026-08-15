"""End-to-end smoke test run by Blender in background mode."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import bpy


def create_mesh_object(name, vertices, faces, edges=()):
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(vertices, edges, faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main():
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension-stage", required=True)
    options = parser.parse_args(arguments)

    stage = Path(options.extension_stage).resolve()
    manifest = (stage / "blender_manifest.toml").read_text(encoding="utf-8")
    assert 'version = "0.3.1"' in manifest
    assert 'license = ["SPDX:GPL-3.0-or-later"]' in manifest
    assert (stage / "LICENSE").read_text(encoding="utf-8").startswith(
        "Yarn-level Knitwear Solver\nCopyright (C) 2026 ysk424"
    )
    assert (stage / "THIRD_PARTY_NOTICES.md").is_file()
    sys.path.insert(0, str(stage.parent))
    import yarn_level_knitware_solver
    from yarn_level_knitware_solver.i18n import tr
    from yarn_level_knitware_solver.native import get_library

    yarn_level_knitware_solver.register()
    try:
        assert get_library().openmp_enabled
        preferences = bpy.context.preferences.view
        old_language = preferences.language
        old_translate_interface = preferences.use_translate_interface
        try:
            preferences.language = "ja_JP"
            preferences.use_translate_interface = True
            assert tr("Bake Simulation") == "シミュレーションをベイク"
            assert tr(
                "Frame {frame} / {end} ({percent:.1f}%)",
                frame=12,
                end=24,
                percent=50.0,
            ) == "フレーム 12 / 24（50.0%）"
        finally:
            preferences.language = old_language
            preferences.use_translate_interface = old_translate_interface
        static = create_mesh_object(
            "YLKS_Test_STATIC",
            [
                (-2, -2, 0),
                (2, -2, 0),
                (2, 2, 0),
                (-2, 2, 0),
                (10, 10, 0),
                (10.0001, 10, 0),
                (10, 10.0001, 0),
                (20, 20, 2),
                (21, 20, 2),
                (20, 21, 2),
            ],
            [(0, 1, 2), (0, 2, 3), (4, 5, 6), (7, 8, 9)],
        )
        shell = create_mesh_object(
            "YLKS_Test_SHELL",
            [
                (-0.5, -0.5, 0.5),
                (0.5, -0.5, 0.5),
                (0.5, 0.5, 0.7),
                (-0.5, 0.5, 0.5),
                (0.502, -0.5, 0.5),
                (1.502, -0.5, 0.5),
                (1.502, 0.5, 0.5),
                (0.502, 0.5, 0.7),
            ],
            [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)],
            edges=[(1, 4), (2, 7)],
        )
        seam_attribute = shell.data.attributes.new(
            name="yohsai_zozo_stitch", type="BOOLEAN", domain="EDGE"
        )
        explicit_pairs = {(1, 4), (2, 7)}
        for edge in shell.data.edges:
            pair = tuple(sorted(int(index) for index in edge.vertices))
            seam_attribute.data[edge.index].value = pair in explicit_pairs
        static.location.z = 0.1
        static.keyframe_insert(data_path="location", index=2, frame=1)
        static.location.z = 0.2
        static.keyframe_insert(data_path="location", index=2, frame=24)
        static.location.z = 0.1
        static_smooth = static.modifiers.new(
            name="Animated Body Deformation", type="SMOOTH"
        )
        static_smooth.factor = 0.0
        shell.location = (0.2, -0.1, 0.3)
        shell.shape_key_add(name="User Basis")
        smooth = shell.modifiers.new(name="Evaluated Initial Shape", type="SMOOTH")
        smooth.factor = 0.5
        smooth.iterations = 1
        smooth.use_x = False
        smooth.use_y = False
        smooth.use_z = True

        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = shell.evaluated_get(depsgraph)
        evaluated_mesh = evaluated.to_mesh(
            preserve_all_data_layers=True,
            depsgraph=depsgraph,
        )
        try:
            expected_shell_positions = [
                tuple(evaluated.matrix_world @ vertex.co)
                for vertex in evaluated_mesh.vertices
            ]
        finally:
            evaluated.to_mesh_clear()

        settings = bpy.context.scene.ylks_settings
        assert settings.substeps == 6
        assert settings.pd_iterations == 10
        assert settings.pcg_iterations == 120
        assert settings.seam_stiffness == 1000000.0
        assert settings.seam_attribute == "yohsai_zozo_stitch"
        assert settings.strain_limit_enabled
        assert settings.strain_limit_percent == 5.0
        settings.shell_object = shell
        settings.static_object = static
        settings.static_crop_enabled = True
        settings.static_crop_min_z = -0.5
        settings.static_crop_max_z = 0.5
        settings.frame_start = 1
        settings.frame_end = 24
        settings.substeps = 4
        settings.thickness = 0.02
        settings.seam_enabled = True
        settings.seam_search_distance = 0.01
        settings.seam_stiffness = 1000000.0

        assert bpy.ops.ylks.prepare() == {"FINISHED"}
        prepared_shell = settings.prepared_shell_object
        prepared_static = settings.prepared_static_object
        prepared_collection = settings.prepared_collection
        assert prepared_shell is not None and prepared_shell != shell
        assert prepared_static is not None and prepared_static != static
        assert prepared_collection is not None
        assert prepared_shell.name in prepared_collection.objects
        assert prepared_static.name in prepared_collection.objects
        assert prepared_static.hide_viewport is False
        assert prepared_static.hide_get() is False
        assert "Animated Body Deformation" in prepared_static.modifiers
        assert "YLKS Static Crop" in prepared_static.modifiers
        prepared_collection_name = prepared_collection.name
        assert tuple(prepared_shell.matrix_world) == tuple(
            type(prepared_shell.matrix_world).Identity(4)
        )
        assert not prepared_shell.modifiers
        assert prepared_shell.data.shape_keys is None
        assert shell.data.shape_keys is not None
        assert shell.data.shape_keys.key_blocks[0].name == "User Basis"
        assert shell.hide_get()
        actual_shell_positions = [tuple(vertex.co) for vertex in prepared_shell.data.vertices]
        assert len(actual_shell_positions) == len(expected_shell_positions)
        for actual, expected in zip(actual_shell_positions, expected_shell_positions):
            assert max(abs(a - b) for a, b in zip(actual, expected)) < 1.0e-6
        prepared_static.data.calc_loop_triangles()
        assert len(prepared_static.data.loop_triangles) == 4
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_static = prepared_static.evaluated_get(depsgraph)
        evaluated_static_mesh = evaluated_static.to_mesh(depsgraph=depsgraph)
        try:
            assert len(evaluated_static_mesh.polygons) == 3
            assert len(evaluated_static_mesh.vertices) == 7
        finally:
            evaluated_static.to_mesh_clear()
        assert settings.last_static_crop_vertices == "7 / 10"
        assert settings.last_static_crop_polygons == "3 / 4"
        assert settings.last_prepare_skipped == 1
        assert settings.last_seam_count == 2
        assert settings.last_seam_source == "attribute yohsai_zozo_stitch"
        seam_pairs = [(1, 4), (2, 7)]
        seam_rest_lengths = [
            math.dist(
                tuple(prepared_shell.data.vertices[a].co),
                tuple(prepared_shell.data.vertices[b].co),
            )
            for a, b in seam_pairs
        ]

        bpy.context.scene.frame_set(7)
        assert bpy.ops.ylks.bake() == {"FINISHED"}
        assert bpy.context.scene.frame_current == 7
        keys = prepared_shell.data.shape_keys
        assert keys is not None
        assert keys.get("ylks_bake_version") == 1
        assert len(keys.key_blocks) == 24
        assert keys.use_relative is False
        final_points = keys.key_blocks[-1].data
        maximum_seam_relative_error = 0.0
        for (a, b), rest_length in zip(seam_pairs, seam_rest_lengths):
            final_length = math.dist(tuple(final_points[a].co), tuple(final_points[b].co))
            relative_error = abs(final_length / rest_length - 1.0)
            maximum_seam_relative_error = max(
                maximum_seam_relative_error, relative_error
            )
            assert relative_error < 0.1
        final_height = min(
            (prepared_shell.matrix_world @ point.co).z
            for point in keys.key_blocks[-1].data
        )
        assert 0.218 <= final_height <= 0.35, final_height
        assert settings.last_status.startswith("Baked 24 frames")
        assert settings.last_status.endswith("cursor restored to frame 7")
        assert not settings.bake_in_progress
        assert math.isclose(settings.bake_progress, 1.0)
        assert settings.bake_current_frame == 24
        assert settings.bake_total_frames == 24
        assert "24" in settings.bake_progress_text
        bpy.context.scene.frame_set(24)
        animated_static_z = prepared_static.matrix_world.translation.z
        assert abs(animated_static_z - 0.2) < 1.0e-6, animated_static_z
        bpy.context.scene.frame_set(1)

        assert bpy.ops.ylks.clear_bake() == {"FINISHED"}
        assert prepared_shell.data.shape_keys is None
        assert shell.data.shape_keys is not None
        assert shell.data.shape_keys.key_blocks[0].name == "User Basis"
        assert not settings.bake_in_progress
        assert settings.bake_progress == 0.0
        assert settings.bake_total_frames == 0
        assert settings.bake_progress_text == "Not started"

        assert bpy.ops.ylks.clear_prepared() == {"FINISHED"}
        assert settings.prepared_shell_object is None
        assert settings.prepared_static_object is None
        assert bpy.data.collections.get(prepared_collection_name) is None
        assert not shell.hide_get()
        print(
            "Blender Extension preparation smoke test passed: "
            f"OpenMP={get_library().openmp_enabled}, skipped_static=1, "
            f"animated_static_delta={animated_static_z - 0.1:.3f}, "
            f"static_crop={settings.last_static_crop_polygons}, "
            f"seam_max_error={maximum_seam_relative_error:.6f}, "
            f"final_min_z={final_height:.6f}"
        )
    finally:
        yarn_level_knitware_solver.unregister()


if __name__ == "__main__":
    main()
