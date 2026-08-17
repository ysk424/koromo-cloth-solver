#include "koromo_cloth_solver.h"

#include <stdio.h>

int main(void) {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    if (desc.struct_size != sizeof(desc) || desc.substeps != 10 ||
        kcsGetAbiVersion() != KCS_ABI_VERSION || !kcsIsOpenMpEnabled()) {
        fputs("C ABI metadata check failed\n", stderr);
        return 1;
    }
    if (!kcsIsCudaEnabled() &&
        (kcsIsCudaAvailable() || kcsGetCudaDeviceName()[0] != '\0')) {
        fputs("C ABI CUDA metadata check failed\n", stderr);
        return 1;
    }
    if (kcsIsCudaEnabled() &&
        (!kcsIsCudaAvailable() || kcsGetCudaDeviceName()[0] == '\0')) {
        fputs("CUDA build has no available device\n", stderr);
        return 1;
    }
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    if (material.struct_size != sizeof(material) || material.strain_limit != 0.0f ||
        material.strain_limit_stiffness <= 0.0f) {
        fputs("C ABI strain-limit defaults check failed\n", stderr);
        return 1;
    }
    KcsSolver *solver = kcsCreate(&desc);
    if (!solver) {
        fprintf(stderr, "C ABI create failed: %s\n", kcsGetLastError(NULL));
        return 1;
    }
    if (kcsSetCudaFallbackAllowed(solver, 1) != KCS_OK ||
        kcsGetExecutionBackend(solver) != KCS_EXECUTION_CPU) {
        fputs("C ABI backend policy check failed\n", stderr);
        kcsDestroy(solver);
        return 1;
    }
    if (kcsSetShellSeams(solver, NULL, 0) != KCS_OK) {
        fprintf(stderr, "C ABI seam clear failed: %s\n", kcsGetLastError(solver));
        kcsDestroy(solver);
        return 1;
    }
    {
        const KcsVec3 vertex = {0.0f, 0.0f, 0.0f};
        if (kcsUpdateStaticVertices(solver, &vertex, 1) !=
            KCS_ERROR_INVALID_STATE) {
            fputs("C ABI animated STATIC state check failed\n", stderr);
            kcsDestroy(solver);
            return 1;
        }
    }
    kcsDestroy(solver);
    puts("C ABI smoke test passed.");
    return 0;
}
