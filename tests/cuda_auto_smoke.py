"""Exercise CUDA selection and lossless AUTO fallback without Blender."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


def grid(side: int):
    vertices = [
        (0.01 * x, 1.0 + 0.0001 * x, 0.01 * z)
        for z in range(side)
        for x in range(side)
    ]
    triangles = []
    for z in range(side - 1):
        for x in range(side - 1):
            a = z * side + x
            b = a + 1
            c = a + side
            d = c + 1
            triangles.extend(((a, c, b), (b, c, d)))
    return vertices, triangles


def configured_solver(library, vertices, triangles, *, stressed: bool):
    from native import Vec3

    desc = library.default_desc()
    desc.gravity = Vec3(0.0, -9.81, 0.0)
    desc.substeps = 1
    desc.pd_iterations = 2
    desc.pcg_iterations = 10 if stressed else 40
    desc.pcg_relative_tolerance = 1.0e-5
    desc.collision_iterations = 0
    material = library.default_material()
    if stressed:
        material.strain_limit = 0.05
        material.strain_limit_stiffness = 100000.0
    solver = library.create(desc)
    solver.set_shell_mesh(vertices, triangles, material)
    solver.set_shell_seams(
        [(0, len(vertices) - 1)] if stressed else [], 100000.0
    )
    solver.build()
    return solver


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", required=True)
    parser.add_argument("--cuda", required=True)
    parser.add_argument("--native-dir", required=True)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(arguments.native_dir).resolve()))
    from native import SolverLibrary

    cpu_library = SolverLibrary(arguments.cpu, backend="CPU")
    auto_library = SolverLibrary(arguments.cuda, backend="AUTO")
    explicit_cuda_library = SolverLibrary(arguments.cuda, backend="CUDA")

    small_vertices, small_triangles = grid(16)
    small = configured_solver(
        auto_library, small_vertices, small_triangles, stressed=False
    )
    assert small.active_backend == "CUDA"
    small.step(1.0 / 60.0)
    assert small.active_backend == "CPU", "small AUTO mesh must use CPU"
    small.close()

    large_vertices, large_triangles = grid(96)
    accelerated = configured_solver(
        auto_library, large_vertices, large_triangles, stressed=False
    )
    accelerated.step(1.0 / 60.0)
    assert accelerated.active_backend == "CUDA", (
        "converged large AUTO mesh must remain on CUDA"
    )
    accelerated_positions = accelerated.positions()
    accelerated.close()

    cuda_reference = configured_solver(
        explicit_cuda_library, large_vertices, large_triangles, stressed=False
    )
    cuda_reference.step(1.0 / 60.0)
    assert cuda_reference.active_backend == "CUDA"
    assert max(
        math.dist(a, b)
        for a, b in zip(accelerated_positions, cuda_reference.positions())
    ) == 0.0
    cuda_reference.close()

    cpu = configured_solver(
        cpu_library, large_vertices, large_triangles, stressed=True
    )
    cpu.step(1.0 / 60.0)
    cpu_positions = cpu.positions()
    cpu.close()

    fallback = configured_solver(
        auto_library, large_vertices, large_triangles, stressed=True
    )
    fallback.step(1.0 / 60.0)
    assert fallback.active_backend == "CPU", (
        "non-converged CUDA PCG must fall back to CPU"
    )
    difference = max(
        math.dist(a, b) for a, b in zip(cpu_positions, fallback.positions())
    )
    assert difference == 0.0, f"AUTO fallback changed positions by {difference}"
    fallback.close()
    print("CUDA AUTO selection and lossless CPU fallback passed")


if __name__ == "__main__":
    run()
