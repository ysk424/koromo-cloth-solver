# koromo-cloth-solver

Koromo is a Windows DLL and Blender Extension for treating a triangulated
garment as a continuum cloth shell. This implementation is **not** a yarn/rod-
level reproduction of the SIGGRAPH 2026 paper. It provides the Blender-facing
shell, animated body collider, explicit seam threads and iterative PD/ADMM
foundation needed before replacing individual material projections with
Nested Douglas--Rachford projections.

The native core, public C header and tests are all contained in this repository.
No sibling solver repository, solver-source path, or second solver DLL is needed.

## Build and test

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

Output: `build/koromo_cloth_solver.dll`.

To build the installable Blender Extension ZIP with the ZIP-distribution
Blender 5.2 executable:

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DKOROMO_BLENDER_EXECUTABLE=C:/Users/azoo/git/build_windows_Release_x64_vc17_Release/bin/blender.exe
cmake --build build --target blender-extension-test
cmake --build build --target blender-extension
```

Output:
`build/packages/koromo_cloth_solver-0.6.0-windows-x64.zip`.

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

Animated BODY contact uses both the previous and current substep geometry.
When a moving body surface sweeps through a nearly stationary garment vertex,
Koromo preserves the vertex's previous side and pushes it along with the body.
Substeps still control simulation accuracy, but body motion no longer has to be
smaller than the cloth thickness merely to avoid collider tunnelling.

The triangle strain limit is two-sided. A five-percent setting projects both
tension and compression to principal stretches in `[0.95, 1.05]`. This keeps
contact loads from progressively reducing garment surface area; visible size
can still change when the preserved surface folds or drapes in 3D.

## Blender Extension workflow

The Extension accepts either one mesh object or a HOU clothes collection. A
HOU collection must carry `housei_role = clothes` and a verified
`housei_sewing_plan_json` using `housei-sewing-plan/1.x`. Koromo validates the
part fingerprints, combines their world-space meshes in plan order, and uses
the plan's exact seam vertex pairs. It imports no Housei or Ominaeshi Python
module and never modifies the HOU source parts.

Each seam pair is a finite-stiffness stitch with a zero-distance target. This
accepts the normal MD/HOU representation where two distinct sewn boundary
vertices already occupy exactly the same position and keeps them tied after
the simulation starts.

Koromo creates a world-space garment snapshot and a separate animated body
copy. The body copy retains deformation modifiers and is cropped to the
world-Z range 0.40--1.45 m by default. Since contact is two-sided, the cropped
collider does not need caps or parity-based inside/outside classification.

In single-object mode, a Boolean EDGE attribute named
`yohsai_zozo_stitch` is read before the proximity fallback. Every marked edge
provides one explicit seam vertex pair. Results are written only to absolute
Shape Keys on the prepared garment copy.

The sidebar follows Blender's interface language setting: `ja_JP` displays the
included Japanese UI, and other languages fall back to English. During an
interactive Bake, only the progress bar in the initiating sidebar Screen is
redrawn. The evaluation frame is restored before each redraw, so other Blender
windows and 3D views do not display intermediate simulation state.

Adaptive BODY substeps are enabled by default. Koromo compares consecutive
evaluated BODY meshes and uses the maximum vertex displacement as its motion
metric. When that displacement is large relative to the garment's smaller
edge scale, only that frame is sampled at Blender subframes, up to the
configured maximum effective substep count. Normal frames keep the configured
base substep count.

Current collision is two-sided cloth-vertex/body-triangle contact. Shell
self-collision is intentionally out of scope for this workflow; exact CCD and
the paper's yarn-scale twisting/contact terms are also not implemented.

## License

Koromo is free software under GNU GPL version 3 or later.
The implementation is independent of the referenced paper's source code.
Bundled native runtime and MIT-core notices are preserved in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

The corresponding source for a distributed DLL is this repository. The native
core was derived from `ysk424/omp-contact-solver` commit
`ddf5cfae1c74266082e5c0da18aa1f53c78e6b05`; its MIT notice is retained in the
third-party notices.

日本語の現状、再現手順、仕様差、次回作業は
[`docs/HANDOFF_JA.md`](docs/HANDOFF_JA.md) を参照してください。

大学生向けの日本語・英語による仕組みの解説と、OpenMP／ビルドパスの説明は
[`docs/SOLVER_OVERVIEW_JA_EN.md`](docs/SOLVER_OVERVIEW_JA_EN.md) にあります。
