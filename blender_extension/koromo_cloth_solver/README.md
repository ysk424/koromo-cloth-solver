# Koromo for Blender

This Windows x64 Blender Extension bakes a stitched continuum cloth shell
against a topology-stable animated body collider. The bundled CPU/OpenMP DLL
writes results as absolute Shape Keys; source objects are not modified.

1. Install the generated ZIP using **Edit > Preferences > Extensions > Install
   from Disk**.
2. Open **3D View > Sidebar > Koromo**. With Blender's interface language set
   to Japanese, the tab and panel name are shown as **衣**.
3. Choose **Mesh Object** for one garment `SHELL`, or **HOU Collection** for
   a HOU clothes collection, then assign the animated body as `BODY`.
4. Keep **Crop BODY** enabled to use the world-Z range 0.40–1.45 m, then run
   **Prepare Simulation Copies**.
5. Choose the bake frames and run **Bake Simulation**.

The sidebar follows Blender's interface language preference and includes
English and Japanese (`ja_JP`). The **Bake Progress** box and Blender status
area show the current frame and completion percentage while the synchronous
bake is running.

HOU mode reads all parts and exact seam pairs from
`housei_sewing_plan_json`. It validates the HOU fingerprints, creates one
solver-owned world-space shell, and leaves every source part unchanged. A
stitch has a zero-distance target, so already coincident MD seam vertices are
valid and remain joined during simulation.

In Mesh Object mode, if the garment has a Boolean EDGE attribute named
`yohsai_zozo_stitch`, every marked loose edge is used as an explicit sewing
pair. Otherwise, the Extension can pair nearby boundary vertices from
disconnected panels. Sewing uses finite Projective Dynamics constraints and
does not merge vertices, preserving panel UVs and material boundaries.

The prepared BODY copy retains Armature and other deformation modifiers. Its
evaluated topology must remain stable throughout the bake. The cropped body
can have open boundaries because collision is two-sided and does not use
inside/outside parity.

Current limitations include no pins, edge-edge contact, exact CCD, yarn rods,
or yarn twisting/sliding terms. Self-collision is intentionally not part of
the current HOU workflow.

## License

This Extension is licensed under GNU GPL version 3 or later. Third-party
notices for the native solver core and statically linked Windows runtimes are
included in `THIRD_PARTY_NOTICES.md`.

Corresponding source is available from
`https://github.com/ysk424/koromo-cloth-solver`. The native core was derived
from `ysk424/omp-contact-solver` commit
`ddf5cfae1c74266082e5c0da18aa1f53c78e6b05`; its MIT notice is retained.
