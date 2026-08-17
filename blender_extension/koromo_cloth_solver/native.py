"""Small ctypes binding for the Koromo C ABI.

This module deliberately has no bpy dependency so the DLL boundary can also be
smoke-tested with a normal Python interpreter.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Iterable, Sequence


KCS_ABI_VERSION = 4
KCS_OK = 0


class NativeSolverError(RuntimeError):
    """Raised when the native solver rejects an operation."""


class Vec3(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]


class Triangle(ctypes.Structure):
    _fields_ = [
        ("i0", ctypes.c_uint32),
        ("i1", ctypes.c_uint32),
        ("i2", ctypes.c_uint32),
    ]


class Seam(ctypes.Structure):
    _fields_ = [
        ("i0", ctypes.c_uint32),
        ("i1", ctypes.c_uint32),
        ("stiffness", ctypes.c_float),
    ]


class SolverDesc(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("gravity", Vec3),
        ("substeps", ctypes.c_uint32),
        ("pd_iterations", ctypes.c_uint32),
        ("pcg_iterations", ctypes.c_uint32),
        ("pcg_relative_tolerance", ctypes.c_float),
        ("collision_iterations", ctypes.c_uint32),
        ("velocity_damping", ctypes.c_float),
        ("thread_count", ctypes.c_uint32),
    ]


class ShellMaterial(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("density", ctypes.c_float),
        ("stretch_stiffness", ctypes.c_float),
        ("bend_stiffness", ctypes.c_float),
        ("thickness", ctypes.c_float),
        ("friction", ctypes.c_float),
        ("restitution", ctypes.c_float),
        ("strain_limit", ctypes.c_float),
        ("strain_limit_stiffness", ctypes.c_float),
    ]


class StepStats(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("substeps", ctypes.c_uint32),
        ("pd_iterations", ctypes.c_uint32),
        ("pcg_iterations", ctypes.c_uint64),
        ("contact_count", ctypes.c_uint64),
        ("final_pcg_relative_residual", ctypes.c_float),
        ("strain_limit_projection_count", ctypes.c_uint64),
        ("maximum_principal_stretch", ctypes.c_float),
    ]


def bundled_library_path(backend: str = "CPU") -> Path:
    """Return the platform-specific CPU or CUDA library path."""
    suffix = "_cuda" if backend.upper() == "CUDA" else ""
    if sys.platform == "win32":
        filename = f"koromo_cloth_solver{suffix}.dll"
    elif sys.platform == "darwin":
        filename = f"libkoromo_cloth_solver{suffix}.dylib"
    else:
        filename = f"libkoromo_cloth_solver{suffix}.so"
    return Path(__file__).resolve().parent / "bin" / filename


def _as_vec3_array(values: Iterable[Sequence[float]]):
    converted = [Vec3(float(value[0]), float(value[1]), float(value[2])) for value in values]
    return (Vec3 * len(converted))(*converted)


def _as_triangle_array(values: Iterable[Sequence[int]]):
    converted = [
        Triangle(int(value[0]), int(value[1]), int(value[2])) for value in values
    ]
    return (Triangle * len(converted))(*converted)


def _as_seam_array(values: Iterable[Sequence[int]], stiffness: float):
    converted = [
        Seam(int(value[0]), int(value[1]), float(stiffness)) for value in values
    ]
    return (Seam * len(converted))(*converted)


class SolverLibrary:
    """Loaded DLL and fully declared C function table."""

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        backend: str = "AUTO",
    ):
        requested = backend.upper()
        if requested not in {"AUTO", "CPU", "CUDA"}:
            raise NativeSolverError(f"Unknown solver backend: {backend}")
        self.requested_backend = requested
        self.fallback_reason = ""
        if path is not None:
            self._load(Path(path))
            self._validate_loaded_library(
                require_cuda=(requested == "CUDA" or
                              (requested == "AUTO" and self.cuda_enabled))
            )
            return

        if requested in {"AUTO", "CUDA"}:
            cuda_path = bundled_library_path("CUDA")
            try:
                self._load(cuda_path)
                self._validate_loaded_library(require_cuda=True)
                return
            except NativeSolverError as exc:
                if requested == "CUDA":
                    raise
                self.fallback_reason = str(exc)

        self._load(bundled_library_path("CPU"))
        self._validate_loaded_library(require_cuda=False)

    def _load(self, path: Path) -> None:
        self.path = path
        if not self.path.is_file():
            raise NativeSolverError(f"Solver library was not found: {self.path}")
        dll_directory = None
        try:
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                dll_directory = os.add_dll_directory(str(self.path.parent))
            self.api = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise NativeSolverError(f"Could not load solver library {self.path}: {exc}") from exc
        finally:
            if dll_directory is not None:
                dll_directory.close()
        self._declare_functions()

    def _validate_loaded_library(self, *, require_cuda: bool) -> None:
        abi = int(self.api.kcsGetAbiVersion())
        if abi != KCS_ABI_VERSION:
            raise NativeSolverError(
                f"Solver ABI mismatch: Extension expects {KCS_ABI_VERSION}, DLL reports {abi}"
            )
        if require_cuda:
            if not self.cuda_enabled:
                raise NativeSolverError(f"Solver DLL has no CUDA backend: {self.path}")
            if not self.cuda_available:
                raise NativeSolverError(
                    f"CUDA is unavailable for {self.path.name}"
                )
        elif self.cuda_enabled:
            raise NativeSolverError(f"Expected CPU solver DLL, got CUDA: {self.path}")

    def _declare_functions(self) -> None:
        api = self.api
        api.kcsGetAbiVersion.argtypes = []
        api.kcsGetAbiVersion.restype = ctypes.c_uint32
        api.kcsIsOpenMpEnabled.argtypes = []
        api.kcsIsOpenMpEnabled.restype = ctypes.c_int32
        api.kcsIsCudaEnabled.argtypes = []
        api.kcsIsCudaEnabled.restype = ctypes.c_int32
        api.kcsIsCudaAvailable.argtypes = []
        api.kcsIsCudaAvailable.restype = ctypes.c_int32
        api.kcsGetCudaDeviceName.argtypes = []
        api.kcsGetCudaDeviceName.restype = ctypes.c_char_p
        api.kcsGetExecutionBackend.argtypes = [ctypes.c_void_p]
        api.kcsGetExecutionBackend.restype = ctypes.c_int32
        api.kcsSetCudaFallbackAllowed.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        api.kcsSetCudaFallbackAllowed.restype = ctypes.c_int32
        api.kcsDefaultSolverDesc.argtypes = [ctypes.POINTER(SolverDesc)]
        api.kcsDefaultSolverDesc.restype = None
        api.kcsDefaultShellMaterial.argtypes = [ctypes.POINTER(ShellMaterial)]
        api.kcsDefaultShellMaterial.restype = None
        api.kcsCreate.argtypes = [ctypes.POINTER(SolverDesc)]
        api.kcsCreate.restype = ctypes.c_void_p
        api.kcsDestroy.argtypes = [ctypes.c_void_p]
        api.kcsDestroy.restype = None
        api.kcsSetStaticMesh.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Vec3),
            ctypes.c_uint32,
            ctypes.POINTER(Triangle),
            ctypes.c_uint32,
        ]
        api.kcsSetStaticMesh.restype = ctypes.c_int32
        api.kcsUpdateStaticVertices.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Vec3),
            ctypes.c_uint32,
        ]
        api.kcsUpdateStaticVertices.restype = ctypes.c_int32
        api.kcsSetShellMesh.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Vec3),
            ctypes.c_uint32,
            ctypes.POINTER(Triangle),
            ctypes.c_uint32,
            ctypes.POINTER(ShellMaterial),
        ]
        api.kcsSetShellMesh.restype = ctypes.c_int32
        api.kcsSetShellSeams.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Seam),
            ctypes.c_uint32,
        ]
        api.kcsSetShellSeams.restype = ctypes.c_int32
        api.kcsBuild.argtypes = [ctypes.c_void_p]
        api.kcsBuild.restype = ctypes.c_int32
        api.kcsStep.argtypes = [ctypes.c_void_p, ctypes.c_float]
        api.kcsStep.restype = ctypes.c_int32
        api.kcsGetShellVertexCount.argtypes = [ctypes.c_void_p]
        api.kcsGetShellVertexCount.restype = ctypes.c_uint32
        api.kcsCopyShellPositions.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Vec3),
            ctypes.c_uint32,
        ]
        api.kcsCopyShellPositions.restype = ctypes.c_int32
        api.kcsGetLastStepStats.argtypes = [ctypes.c_void_p, ctypes.POINTER(StepStats)]
        api.kcsGetLastStepStats.restype = ctypes.c_int32
        api.kcsGetLastError.argtypes = [ctypes.c_void_p]
        api.kcsGetLastError.restype = ctypes.c_char_p

    @property
    def openmp_enabled(self) -> bool:
        return bool(self.api.kcsIsOpenMpEnabled())

    @property
    def cuda_enabled(self) -> bool:
        return bool(self.api.kcsIsCudaEnabled())

    @property
    def cuda_available(self) -> bool:
        return bool(self.api.kcsIsCudaAvailable())

    @property
    def cuda_device_name(self) -> str:
        raw = self.api.kcsGetCudaDeviceName()
        return raw.decode("utf-8", errors="replace") if raw else ""

    @property
    def active_backend(self) -> str:
        return "CUDA" if self.cuda_enabled else "CPU"

    def default_desc(self) -> SolverDesc:
        value = SolverDesc()
        self.api.kcsDefaultSolverDesc(ctypes.byref(value))
        return value

    def default_material(self) -> ShellMaterial:
        value = ShellMaterial()
        self.api.kcsDefaultShellMaterial(ctypes.byref(value))
        return value

    def create(self, desc: SolverDesc) -> "Solver":
        return Solver(self, desc)


class Solver:
    """RAII-style wrapper around one opaque KcsSolver handle."""

    def __init__(self, library: SolverLibrary, desc: SolverDesc):
        self.library = library
        self.handle = library.api.kcsCreate(ctypes.byref(desc))
        if not self.handle:
            raise NativeSolverError("Could not create the native solver")
        self._check(
            self.library.api.kcsSetCudaFallbackAllowed(
                self.handle,
                1 if self.library.requested_backend == "AUTO" else 0,
            ),
            "Setting CUDA fallback policy",
        )

    def close(self) -> None:
        if self.handle:
            self.library.api.kcsDestroy(self.handle)
            self.handle = None

    def __enter__(self) -> "Solver":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _error_text(self) -> str:
        raw = self.library.api.kcsGetLastError(self.handle)
        return raw.decode("utf-8", errors="replace") if raw else "unknown native error"

    def _check(self, result: int, operation: str) -> None:
        if int(result) != KCS_OK:
            raise NativeSolverError(
                f"{operation} failed with result {int(result)}: {self._error_text()}"
            )

    def set_static_mesh(self, vertices, triangles) -> None:
        vertex_array = _as_vec3_array(vertices)
        triangle_array = _as_triangle_array(triangles)
        result = self.library.api.kcsSetStaticMesh(
            self.handle,
            vertex_array if len(vertex_array) else None,
            len(vertex_array),
            triangle_array if len(triangle_array) else None,
            len(triangle_array),
        )
        self._check(result, "Setting STATIC mesh")

    def update_static_vertices(self, vertices) -> None:
        vertex_array = _as_vec3_array(vertices)
        result = self.library.api.kcsUpdateStaticVertices(
            self.handle,
            vertex_array if len(vertex_array) else None,
            len(vertex_array),
        )
        self._check(result, "Updating animated STATIC vertices")

    def set_shell_mesh(self, vertices, triangles, material: ShellMaterial) -> None:
        vertex_array = _as_vec3_array(vertices)
        triangle_array = _as_triangle_array(triangles)
        result = self.library.api.kcsSetShellMesh(
            self.handle,
            vertex_array,
            len(vertex_array),
            triangle_array,
            len(triangle_array),
            ctypes.byref(material),
        )
        self._check(result, "Setting SHELL mesh")

    def set_shell_seams(self, seams, stiffness: float) -> None:
        seam_array = _as_seam_array(seams, stiffness)
        result = self.library.api.kcsSetShellSeams(
            self.handle,
            seam_array if len(seam_array) else None,
            len(seam_array),
        )
        self._check(result, "Setting SHELL seams")

    def build(self) -> None:
        self._check(self.library.api.kcsBuild(self.handle), "Building solver")

    def step(self, frame_dt: float) -> None:
        self._check(
            self.library.api.kcsStep(self.handle, ctypes.c_float(frame_dt)),
            "Stepping solver",
        )

    def positions(self) -> list[tuple[float, float, float]]:
        count = int(self.library.api.kcsGetShellVertexCount(self.handle))
        values = (Vec3 * count)()
        self._check(
            self.library.api.kcsCopyShellPositions(self.handle, values, count),
            "Copying SHELL positions",
        )
        return [(float(value.x), float(value.y), float(value.z)) for value in values]

    def stats(self) -> StepStats:
        value = StepStats()
        value.struct_size = ctypes.sizeof(StepStats)
        self._check(
            self.library.api.kcsGetLastStepStats(self.handle, ctypes.byref(value)),
            "Reading solver statistics",
        )
        return value

    @property
    def active_backend(self) -> str:
        value = int(self.library.api.kcsGetExecutionBackend(self.handle))
        return "CUDA" if value == 2 else "CPU"


_libraries: dict[str, SolverLibrary] = {}


def get_library(backend: str = "AUTO") -> SolverLibrary:
    key = backend.upper()
    if key not in _libraries:
        _libraries[key] = SolverLibrary(backend=key)
    return _libraries[key]
