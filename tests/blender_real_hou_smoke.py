"""Run an Ominaeshi-to-Koromo smoke test on a copied real Blender file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bpy


def main():
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--ominaeshi-root", required=True)
    parser.add_argument("--koromo-stage", required=True)
    parser.add_argument("--clothes", required=True)
    parser.add_argument("--body", required=True)
    options = parser.parse_args(arguments)

    sys.path.insert(0, str(Path(options.ominaeshi_root).resolve().parent))
    sys.path.insert(0, str(Path(options.koromo_stage).resolve().parent))
    import ominaeshi
    import koromo_cloth_solver
    from ominaeshi.hou_export import create_hou_collection

    ominaeshi.register()
    koromo_cloth_solver.register()
    try:
        source = bpy.data.objects[options.clothes]
        body = bpy.data.objects[options.body]
        converted = create_hou_collection(bpy.context, source)
        assert converted.pair_count > 0

        settings = bpy.context.scene.koromo_settings
        settings.source_mode = "HOU"
        settings.hou_collection = converted.collection
        settings.static_object = body
        settings.static_crop_enabled = True
        settings.static_crop_min_z = 0.40
        settings.static_crop_max_z = 1.45
        settings.frame_start = 1
        settings.frame_end = 2
        settings.seam_enabled = True
        assert bpy.ops.koromo.prepare() == {"FINISHED"}, settings.last_prepare_status

        shell = settings.prepared_shell_object
        flat = list(shell.get("koromo_seam_pairs", ()))
        points = [shell.matrix_world @ vertex.co for vertex in shell.data.vertices]
        lengths = [
            (points[int(flat[index])] - points[int(flat[index + 1])]).length
            for index in range(0, len(flat), 2)
        ]
        assert len(lengths) == converted.pair_count
        assert max(lengths) <= 1.0e-5
        assert bpy.ops.koromo.bake() == {"FINISHED"}, settings.last_status
        result = {
            "parts": converted.part_count,
            "labels": converted.seam_label_count,
            "pairs": converted.pair_count,
            "maximum_initial_seam_m": max(lengths),
            "status": settings.last_status,
            "contacts": settings.last_contacts,
            "residual": settings.last_residual,
            "strain": settings.last_strain,
        }
        print("REAL_HOU_SMOKE " + json.dumps(result, ensure_ascii=False))
    finally:
        koromo_cloth_solver.unregister()
        ominaeshi.unregister()


if __name__ == "__main__":
    main()
