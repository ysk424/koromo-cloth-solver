# Yarn-level-knitware-solver

Windows DLL for treating a triangulated garment as a continuum cloth shell.
Despite the project name, this first implementation is **not** a yarn/rod-level
reproduction of the SIGGRAPH 2026 paper. It provides the Blender-facing shell,
animated body-collider and iterative PD/ADMM foundation needed before replacing
individual material projections with Nested Douglas--Rachford projections.

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

## Blender data flow

`blender_bridge/native.py` has no `bpy` dependency and can be copied into a
Blender Extension. Use evaluated, triangulated meshes in world coordinates:
The bridge configures gravity as Blender Z-down (`0, 0, -9.81`).

```python
from .native import ClothSolver

solver = ClothSolver(substeps=10, pd_iterations=8, pcg_iterations=80)
solver.set_body(body_vertices, body_triangles)
solver.set_cloth(cloth_vertices, cloth_triangles)
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

Current collision is two-sided cloth-vertex/body-triangle contact. Shell
self-collision, exact CCD and the paper's yarn-scale twisting/contact terms are
not yet implemented.

日本語の現状、再現手順、仕様差、次回作業は
[`docs/HANDOFF_JA.md`](docs/HANDOFF_JA.md) を参照してください。
