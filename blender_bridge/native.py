"""ctypes bridge for yarn_level_knitware_solver.dll (Blender-safe, no bpy import)."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

ABI_VERSION = 4
OK = 0


class Vec3(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]


class Triangle(ctypes.Structure):
    _fields_ = [("i0", ctypes.c_uint32), ("i1", ctypes.c_uint32), ("i2", ctypes.c_uint32)]


class SolverDesc(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("gravity", Vec3),
        ("substeps", ctypes.c_uint32), ("pd_iterations", ctypes.c_uint32),
        ("pcg_iterations", ctypes.c_uint32), ("pcg_relative_tolerance", ctypes.c_float),
        ("collision_iterations", ctypes.c_uint32), ("velocity_damping", ctypes.c_float),
        ("thread_count", ctypes.c_uint32),
    ]


class ShellMaterial(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("density", ctypes.c_float),
        ("stretch_stiffness", ctypes.c_float), ("bend_stiffness", ctypes.c_float),
        ("thickness", ctypes.c_float), ("friction", ctypes.c_float),
        ("restitution", ctypes.c_float), ("strain_limit", ctypes.c_float),
        ("strain_limit_stiffness", ctypes.c_float),
    ]


class StepStats(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("substeps", ctypes.c_uint32),
        ("pd_iterations", ctypes.c_uint32), ("pcg_iterations", ctypes.c_uint64),
        ("contact_count", ctypes.c_uint64),
        ("final_pcg_relative_residual", ctypes.c_float),
        ("strain_limit_projection_count", ctypes.c_uint64),
        ("maximum_principal_stretch", ctypes.c_float),
    ]


def _vecs(values):
    data = [Vec3(float(v[0]), float(v[1]), float(v[2])) for v in values]
    return (Vec3 * len(data))(*data)


def _tris(values):
    data = [Triangle(int(v[0]), int(v[1]), int(v[2])) for v in values]
    return (Triangle * len(data))(*data)


class ClothSolver:
    """One cloth shell and one topology-stable animated body collider."""

    def __init__(self, dll_path=None, *, gravity=(0.0, 0.0, -9.81),
                 substeps=None, pd_iterations=None, pcg_iterations=None,
                 thread_count=None):
        path = (Path(dll_path) if dll_path else
                Path(__file__).parent.parent / "yarn_level_knitware_solver.dll")
        path = path.resolve()
        if not path.is_file():
            raise RuntimeError(f"solver DLL not found: {path}")
        dll_dir = None
        try:
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                dll_dir = os.add_dll_directory(str(path.parent))
            self.lib = ctypes.CDLL(str(path))
        finally:
            if dll_dir is not None:
                dll_dir.close()
        self._bind()
        if self.lib.ocsGetAbiVersion() != ABI_VERSION:
            raise RuntimeError("yarn solver ABI mismatch")
        desc = SolverDesc()
        self.lib.ocsDefaultSolverDesc(ctypes.byref(desc))
        desc.gravity = Vec3(*map(float, gravity))
        if substeps is not None:
            desc.substeps = int(substeps)
        if pd_iterations is not None:
            desc.pd_iterations = int(pd_iterations)
        if pcg_iterations is not None:
            desc.pcg_iterations = int(pcg_iterations)
        if thread_count is not None:
            desc.thread_count = int(thread_count)
        self.handle = self.lib.ocsCreate(ctypes.byref(desc))
        if not self.handle:
            raise RuntimeError("could not create yarn cloth solver")

    def _bind(self):
        lib = self.lib
        lib.ocsGetAbiVersion.argtypes = []
        lib.ocsGetAbiVersion.restype = ctypes.c_uint32
        lib.ocsDefaultSolverDesc.argtypes = [ctypes.POINTER(SolverDesc)]
        lib.ocsDefaultSolverDesc.restype = None
        lib.ocsCreate.argtypes = [ctypes.POINTER(SolverDesc)]; lib.ocsCreate.restype = ctypes.c_void_p
        lib.ocsDestroy.argtypes = [ctypes.c_void_p]
        lib.ocsDefaultShellMaterial.argtypes = [ctypes.POINTER(ShellMaterial)]
        lib.ocsDefaultShellMaterial.restype = None
        mesh_args = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32,
                     ctypes.POINTER(Triangle), ctypes.c_uint32]
        lib.ocsSetStaticMesh.argtypes = mesh_args; lib.ocsSetStaticMesh.restype = ctypes.c_int32
        lib.ocsSetShellMesh.argtypes = mesh_args + [ctypes.POINTER(ShellMaterial)]
        lib.ocsSetShellMesh.restype = ctypes.c_int32
        lib.ocsUpdateStaticVertices.argtypes = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32]
        lib.ocsUpdateStaticVertices.restype = ctypes.c_int32
        lib.ocsBuild.argtypes = [ctypes.c_void_p]; lib.ocsBuild.restype = ctypes.c_int32
        lib.ocsStep.argtypes = [ctypes.c_void_p, ctypes.c_float]; lib.ocsStep.restype = ctypes.c_int32
        lib.ocsGetShellVertexCount.argtypes = [ctypes.c_void_p]; lib.ocsGetShellVertexCount.restype = ctypes.c_uint32
        lib.ocsCopyShellPositions.argtypes = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32]
        lib.ocsCopyShellPositions.restype = ctypes.c_int32
        lib.ocsGetLastError.argtypes = [ctypes.c_void_p]; lib.ocsGetLastError.restype = ctypes.c_char_p
        lib.ocsGetLastStepStats.argtypes = [ctypes.c_void_p, ctypes.POINTER(StepStats)]
        lib.ocsGetLastStepStats.restype = ctypes.c_int32

    def _check(self, code, operation):
        if code != OK:
            raw = self.lib.ocsGetLastError(self.handle)
            message = raw.decode("utf-8", "replace") if raw else "unknown error"
            raise RuntimeError(f"{operation}: {message} ({code})")

    def default_material(self):
        material = ShellMaterial()
        self.lib.ocsDefaultShellMaterial(ctypes.byref(material))
        return material

    def set_body(self, vertices, triangles):
        v, t = _vecs(vertices), _tris(triangles)
        self._check(self.lib.ocsSetStaticMesh(self.handle, v, len(v), t, len(t)), "set body")

    def set_cloth(self, vertices, triangles, material=None):
        v, t = _vecs(vertices), _tris(triangles)
        material = self.default_material() if material is None else material
        self._check(self.lib.ocsSetShellMesh(self.handle, v, len(v), t, len(t), ctypes.byref(material)), "set cloth")

    def build(self):
        self._check(self.lib.ocsBuild(self.handle), "build")

    def update_body(self, vertices):
        v = _vecs(vertices)
        self._check(self.lib.ocsUpdateStaticVertices(self.handle, v, len(v)), "update body")

    def step(self, dt):
        self._check(self.lib.ocsStep(self.handle, ctypes.c_float(dt)), "step")
        count = self.lib.ocsGetShellVertexCount(self.handle)
        out = (Vec3 * count)()
        self._check(self.lib.ocsCopyShellPositions(self.handle, out, count), "fetch cloth")
        return [(p.x, p.y, p.z) for p in out]

    def stats(self):
        value = StepStats()
        value.struct_size = ctypes.sizeof(StepStats)
        self._check(self.lib.ocsGetLastStepStats(self.handle, ctypes.byref(value)), "get stats")
        return value

    def close(self):
        if self.handle:
            self.lib.ocsDestroy(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
