#ifndef KOROMO_CLOTH_SOLVER_H
#define KOROMO_CLOTH_SOLVER_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(KCS_BUILD_DLL)
#    define KCS_API __declspec(dllexport)
#  else
#    define KCS_API __declspec(dllimport)
#  endif
#else
#  define KCS_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define KCS_ABI_VERSION 4u

typedef struct KcsSolver KcsSolver;

typedef struct KcsVec3 {
    float x;
    float y;
    float z;
} KcsVec3;

typedef struct KcsTriangle {
    uint32_t i0;
    uint32_t i1;
    uint32_t i2;
} KcsTriangle;

/* A finite-stiffness stitch between two SHELL vertices. Its target length is
 * zero and it is solved with the other Projective Dynamics constraints. */
typedef struct KcsSeam {
    uint32_t i0;
    uint32_t i1;
    float stiffness;
} KcsSeam;

typedef enum KcsResult {
    KCS_OK = 0,
    KCS_ERROR_INVALID_ARGUMENT = 1,
    KCS_ERROR_INVALID_STATE = 2,
    KCS_ERROR_INVALID_MESH = 3,
    KCS_ERROR_OUT_OF_MEMORY = 4,
    KCS_ERROR_NUMERICAL_FAILURE = 5,
    KCS_ERROR_INTERNAL = 6
} KcsResult;

typedef enum KcsExecutionBackend {
    KCS_EXECUTION_CPU = 1,
    KCS_EXECUTION_CUDA = 2
} KcsExecutionBackend;

/* Solver-wide settings. Set struct_size with kcsDefaultSolverDesc(). */
typedef struct KcsSolverDesc {
    uint32_t struct_size;
    KcsVec3 gravity;
    uint32_t substeps;
    uint32_t pd_iterations;
    uint32_t pcg_iterations;
    float pcg_relative_tolerance;
    uint32_t collision_iterations;
    float velocity_damping;
    uint32_t thread_count; /* 0 selects omp_get_max_threads(). */
} KcsSolverDesc;

/* One material is applied to the complete SHELL mesh. */
typedef struct KcsShellMaterial {
    uint32_t struct_size;
    float density;
    float stretch_stiffness;
    float bend_stiffness;
    float thickness;
    float friction;
    float restitution;
    /* Maximum absolute in-plane principal strain as a fraction (0 disables
     * it). For example, 0.05 projects triangle stretches to [0.95, 1.05]. */
    float strain_limit;
    /* Projective-Dynamics/ADMM penalty weight for strain convergence. */
    float strain_limit_stiffness;
} KcsShellMaterial;

typedef struct KcsStepStats {
    uint32_t struct_size;
    uint32_t substeps;
    uint32_t pd_iterations;
    uint64_t pcg_iterations;
    uint64_t contact_count;
    float final_pcg_relative_residual;
    uint64_t strain_limit_projection_count;
    float maximum_principal_stretch;
} KcsStepStats;

KCS_API uint32_t kcsGetAbiVersion(void);
KCS_API int32_t kcsIsOpenMpEnabled(void);
/* The CPU DLL returns zero for both CUDA queries. The optional CUDA DLL
 * returns one from kcsIsCudaEnabled() and reports whether a usable NVIDIA
 * device is present at runtime from kcsIsCudaAvailable(). */
KCS_API int32_t kcsIsCudaEnabled(void);
KCS_API int32_t kcsIsCudaAvailable(void);
KCS_API const char *kcsGetCudaDeviceName(void);
KCS_API KcsExecutionBackend kcsGetExecutionBackend(const KcsSolver *solver);
/* AUTO clients enable this before kcsBuild(). If a mesh is too small for GPU
 * launch overhead or CUDA PCG does not converge within the configured cap,
 * the untouched frame is recomputed by the CPU backend and remains there. */
KCS_API KcsResult kcsSetCudaFallbackAllowed(KcsSolver *solver,
                                            int32_t allowed);
KCS_API void kcsDefaultSolverDesc(KcsSolverDesc *desc);
KCS_API void kcsDefaultShellMaterial(KcsShellMaterial *material);

KCS_API KcsSolver *kcsCreate(const KcsSolverDesc *desc);
KCS_API void kcsDestroy(KcsSolver *solver);

/* STATIC topology is frozen by kcsBuild(). Passing zero triangles clears it. */
KCS_API KcsResult kcsSetStaticMesh(KcsSolver *solver,
                                   const KcsVec3 *vertices,
                                   uint32_t vertex_count,
                                   const KcsTriangle *triangles,
                                   uint32_t triangle_count);

/* Queues deformed STATIC vertices for the next kcsStep(). The vertex count
 * and triangle topology must match kcsSetStaticMesh(). During that step the
 * STATIC surface is linearly interpolated over the configured substeps and
 * its BVH is refitted. Call only after kcsBuild(). */
KCS_API KcsResult kcsUpdateStaticVertices(KcsSolver *solver,
                                          const KcsVec3 *vertices,
                                          uint32_t vertex_count);

/* Exactly one simulated SHELL mesh is supported. PIN constraints are absent. */
KCS_API KcsResult kcsSetShellMesh(KcsSolver *solver,
                                  const KcsVec3 *vertices,
                                  uint32_t vertex_count,
                                  const KcsTriangle *triangles,
                                  uint32_t triangle_count,
                                  const KcsShellMaterial *material);

/* Optional seam-thread constraints. Call after kcsSetShellMesh() and before
 * kcsBuild(). Passing zero seams clears them. */
KCS_API KcsResult kcsSetShellSeams(KcsSolver *solver,
                                   const KcsSeam *seams,
                                   uint32_t seam_count);

/* Builds SHELL constraints and the refittable STATIC triangle BVH. */
KCS_API KcsResult kcsBuild(KcsSolver *solver);

/* Advances by frame_dt seconds. The configured substeps are internal. */
KCS_API KcsResult kcsStep(KcsSolver *solver, float frame_dt);

KCS_API uint32_t kcsGetShellVertexCount(const KcsSolver *solver);
KCS_API KcsResult kcsCopyShellPositions(const KcsSolver *solver,
                                        KcsVec3 *positions,
                                        uint32_t capacity);
KCS_API KcsResult kcsCopyShellVelocities(const KcsSolver *solver,
                                         KcsVec3 *velocities,
                                         uint32_t capacity);
KCS_API KcsResult kcsGetLastStepStats(const KcsSolver *solver,
                                      KcsStepStats *stats);

/* The returned pointer remains valid until the next call on this solver. */
KCS_API const char *kcsGetLastError(const KcsSolver *solver);

#ifdef __cplusplus
}
#endif

#endif
