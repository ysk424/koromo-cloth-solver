"""ctypes bridge for koromo_cloth_solver.dll (Blender-safe, no bpy import)."""
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


class Seam(ctypes.Structure):
    _fields_ = [
        ("i0", ctypes.c_uint32),
        ("i1", ctypes.c_uint32),
        ("stiffness", ctypes.c_float),
    ]


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


def _seams(values, stiffness):
    data = [Seam(int(v[0]), int(v[1]), float(stiffness)) for v in values]
    return (Seam * len(data))(*data)


class ClothSolver:
    """One cloth shell and one topology-stable animated body collider."""

    def __init__(self, dll_path=None, *, gravity=(0.0, 0.0, -9.81),
                 substeps=None, pd_iterations=None, pcg_iterations=None,
                 thread_count=None):
        path = (Path(dll_path) if dll_path else
                Path(__file__).parent.parent / "koromo_cloth_solver.dll")
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
        if self.lib.kcsGetAbiVersion() != ABI_VERSION:
            raise RuntimeError("Koromo solver ABI mismatch")
        desc = SolverDesc()
        self.lib.kcsDefaultSolverDesc(ctypes.byref(desc))
        desc.gravity = Vec3(*map(float, gravity))
        if substeps is not None:
            desc.substeps = int(substeps)
        if pd_iterations is not None:
            desc.pd_iterations = int(pd_iterations)
        if pcg_iterations is not None:
            desc.pcg_iterations = int(pcg_iterations)
        if thread_count is not None:
            desc.thread_count = int(thread_count)
        self.handle = self.lib.kcsCreate(ctypes.byref(desc))
        if not self.handle:
            raise RuntimeError("could not create Koromo cloth solver")

    def _bind(self):
        lib = self.lib
        lib.kcsGetAbiVersion.argtypes = []
        lib.kcsGetAbiVersion.restype = ctypes.c_uint32
        lib.kcsDefaultSolverDesc.argtypes = [ctypes.POINTER(SolverDesc)]
        lib.kcsDefaultSolverDesc.restype = None
        lib.kcsCreate.argtypes = [ctypes.POINTER(SolverDesc)]; lib.kcsCreate.restype = ctypes.c_void_p
        lib.kcsDestroy.argtypes = [ctypes.c_void_p]
        lib.kcsDefaultShellMaterial.argtypes = [ctypes.POINTER(ShellMaterial)]
        lib.kcsDefaultShellMaterial.restype = None
        mesh_args = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32,
                     ctypes.POINTER(Triangle), ctypes.c_uint32]
        lib.kcsSetStaticMesh.argtypes = mesh_args; lib.kcsSetStaticMesh.restype = ctypes.c_int32
        lib.kcsSetShellMesh.argtypes = mesh_args + [ctypes.POINTER(ShellMaterial)]
        lib.kcsSetShellMesh.restype = ctypes.c_int32
        lib.kcsSetShellSeams.argtypes = [ctypes.c_void_p, ctypes.POINTER(Seam), ctypes.c_uint32]
        lib.kcsSetShellSeams.restype = ctypes.c_int32
        lib.kcsUpdateStaticVertices.argtypes = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32]
        lib.kcsUpdateStaticVertices.restype = ctypes.c_int32
        lib.kcsBuild.argtypes = [ctypes.c_void_p]; lib.kcsBuild.restype = ctypes.c_int32
        lib.kcsStep.argtypes = [ctypes.c_void_p, ctypes.c_float]; lib.kcsStep.restype = ctypes.c_int32
        lib.kcsGetShellVertexCount.argtypes = [ctypes.c_void_p]; lib.kcsGetShellVertexCount.restype = ctypes.c_uint32
        lib.kcsCopyShellPositions.argtypes = [ctypes.c_void_p, ctypes.POINTER(Vec3), ctypes.c_uint32]
        lib.kcsCopyShellPositions.restype = ctypes.c_int32
        lib.kcsGetLastError.argtypes = [ctypes.c_void_p]; lib.kcsGetLastError.restype = ctypes.c_char_p
        lib.kcsGetLastStepStats.argtypes = [ctypes.c_void_p, ctypes.POINTER(StepStats)]
        lib.kcsGetLastStepStats.restype = ctypes.c_int32

    def _check(self, code, operation):
        if code != OK:
            raw = self.lib.kcsGetLastError(self.handle)
            message = raw.decode("utf-8", "replace") if raw else "unknown error"
            raise RuntimeError(f"{operation}: {message} ({code})")

    def default_material(self):
        material = ShellMaterial()
        self.lib.kcsDefaultShellMaterial(ctypes.byref(material))
        return material

    def set_body(self, vertices, triangles):
        v, t = _vecs(vertices), _tris(triangles)
        self._check(self.lib.kcsSetStaticMesh(self.handle, v, len(v), t, len(t)), "set body")

    def set_cloth(self, vertices, triangles, material=None):
        v, t = _vecs(vertices), _tris(triangles)
        material = self.default_material() if material is None else material
        self._check(self.lib.kcsSetShellMesh(self.handle, v, len(v), t, len(t), ctypes.byref(material)), "set cloth")

    def set_seams(self, seams, stiffness=1000000.0):
        values = _seams(seams, stiffness)
        self._check(
            self.lib.kcsSetShellSeams(
                self.handle, values if len(values) else None, len(values)
            ),
            "set seams",
        )

    def build(self):
        self._check(self.lib.kcsBuild(self.handle), "build")

    def update_body(self, vertices):
        v = _vecs(vertices)
        self._check(self.lib.kcsUpdateStaticVertices(self.handle, v, len(v)), "update body")

    def step(self, dt):
        self._check(self.lib.kcsStep(self.handle, ctypes.c_float(dt)), "step")
        count = self.lib.kcsGetShellVertexCount(self.handle)
        out = (Vec3 * count)()
        self._check(self.lib.kcsCopyShellPositions(self.handle, out, count), "fetch cloth")
        return [(p.x, p.y, p.z) for p in out]

    def stats(self):
        value = StepStats()
        value.struct_size = ctypes.sizeof(StepStats)
        self._check(self.lib.kcsGetLastStepStats(self.handle, ctypes.byref(value)), "get stats")
        return value

    def close(self):
        if self.handle:
            self.lib.kcsDestroy(self.handle)
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
