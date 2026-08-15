# Yarn-level-knitware-solver

Windows DLL for treating a triangulated garment as a continuum cloth shell.
Despite the project name, this first implementation is **not** a yarn/rod-level
reproduction of the SIGGRAPH 2026 paper. It provides the Blender-facing shell,
animated body-collider, explicit seam threads and iterative PD/ADMM foundation
needed before replacing individual material projections with Nested
Douglas--Rachford projections.

The native core is compiled into this DLL from the validated sibling
`../omp-contact-solver` source. The resulting DLL is standalone; a second solver
DLL is not loaded at runtime.

## Build and test

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

Output: `build/yarn_level_knitware_solver.dll`.

To build the installable Blender Extension ZIP with the ZIP-distribution
Blender 5.2 executable:

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DYLKS_BLENDER_EXECUTABLE=C:/Users/azoo/git/build_windows_Release_x64_vc17_Release/bin/blender.exe
cmake --build build --target blender-extension-test
cmake --build build --target blender-extension
```

Output:
`build/packages/yarn_level_knitware_solver-0.3.1-windows-x64.zip`.

## Blender data flow

`blender_bridge/native.py` has no `bpy` dependency and can be copied into a
Blender Extension. Use evaluated, triangulated meshes in world coordinates:
The bridge configures gravity as Blender Z-down (`0, 0, -9.81`).

```python
from .native import ClothSolver

solver = ClothSolver(substeps=10, pd_iterations=8, pcg_iterations=80)
solver.set_body(body_vertices, body_triangles)
solver.set_cloth(cloth_vertices, cloth_triangles)
solver.set_seams(seam_vertex_pairs)
solver.build()

# Once per Blender frame; body topology and vertex count must stay unchanged.
solver.update_body(evaluated_body_vertices)
cloth_positions = solver.step(1.0 / scene.render.fps)
print(solver.stats().contact_count)
```

Write `cloth_positions` to a simulation copy of the garment or bake them into
absolute Shape Keys. Do not mutate the user's source mesh. Both inputs must be
triangulated and use the same coordinate space. The body may deform every frame,
but its topology must remain stable.

## Blender Extension workflow

The Extension creates a world-space garment snapshot and a separate animated
body copy. The body copy retains deformation modifiers and is cropped to the
world-Z range 0.40--1.45 m by default. Since contact is two-sided, the cropped
collider does not need caps or parity-based inside/outside classification.

For Yohsai/ZOZO garment data, a Boolean EDGE attribute named
`yohsai_zozo_stitch` is read before the proximity fallback. Every marked edge
provides one explicit seam vertex pair. Results are written only to absolute
Shape Keys on the prepared garment copy.

The sidebar follows Blender's interface language setting: `ja_JP` displays the
included Japanese UI, and other languages fall back to English. During Bake,
the panel progress bar and Blender status area show the current frame and
completion percentage.

Current collision is two-sided cloth-vertex/body-triangle contact. Shell
self-collision, exact CCD and the paper's yarn-scale twisting/contact terms are
not yet implemented.

## License

Yarn-level Knitwear Solver is free software under GNU GPL version 3 or later.
The implementation is independent of the referenced paper's source code.
Bundled native runtime and MIT-core notices are preserved in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

The corresponding source for a distributed DLL is this repository together
with `ysk424/omp-contact-solver` commit
`ddf5cfae1c74266082e5c0da18aa1f53c78e6b05`, which supplies the native files
compiled by `CMakeLists.txt`.

日本語の現状、再現手順、仕様差、次回作業は
[`docs/HANDOFF_JA.md`](docs/HANDOFF_JA.md) を参照してください。

大学生向けの日本語・英語による仕組みの解説と、OpenMP／ビルドパスの説明は
[`docs/SOLVER_OVERVIEW_JA_EN.md`](docs/SOLVER_OVERVIEW_JA_EN.md) にあります。
