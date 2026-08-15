#include "koromo_cloth_solver.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

namespace {

void require(bool condition, const char *message) {
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        std::exit(1);
    }
}

void require_ok(KcsResult result, KcsSolver *solver, const char *operation) {
    if (result != KCS_OK) {
        std::fprintf(stderr, "FAIL: %s: %s\n", operation, kcsGetLastError(solver));
        std::exit(1);
    }
}

float distance(KcsVec3 a, KcsVec3 b) {
    const float x = a.x - b.x;
    const float y = a.y - b.y;
    const float z = a.z - b.z;
    return std::sqrt(x * x + y * y + z * z);
}

void test_free_fall() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.substeps = 2;
    desc.pd_iterations = 4;
    desc.thread_count = 2;
    KcsSolver *solver = kcsCreate(&desc);
    require(solver != nullptr, "create free-fall solver");

    const KcsVec3 vertices[] = {
        {-0.5f, 1.0f, 0.0f}, {0.5f, 1.0f, 0.0f}, {0.0f, 1.0f, 0.8f}};
    const KcsTriangle triangles[] = {{0, 1, 2}};
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    require_ok(kcsSetShellMesh(solver, vertices, 3, triangles, 1, &material),
               solver, "set free-fall SHELL");
    require_ok(kcsBuild(solver), solver, "build free-fall solver");
    require_ok(kcsStep(solver, 1.0f / 60.0f), solver, "free-fall step");

    KcsVec3 result[3];
    require_ok(kcsCopyShellPositions(solver, result, 3), solver,
               "copy free-fall positions");
    require(result[0].y < vertices[0].y, "gravity must move SHELL downward");
    require(std::abs(distance(result[0], result[1]) - 1.0f) < 2.0e-3f,
            "stretch constraint must preserve a rest edge");
    kcsDestroy(solver);
}

void test_static_floor_contact() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.substeps = 4;
    desc.pd_iterations = 6;
    desc.pcg_iterations = 60;
    desc.thread_count = 4;
    KcsSolver *solver = kcsCreate(&desc);
    require(solver != nullptr, "create contact solver");

    const KcsVec3 floor_vertices[] = {
        {-3.0f, 0.0f, -3.0f}, {3.0f, 0.0f, -3.0f},
        {3.0f, 0.0f, 3.0f}, {-3.0f, 0.0f, 3.0f}};
    const KcsTriangle floor_triangles[] = {{0, 2, 1}, {0, 3, 2}};
    require_ok(kcsSetStaticMesh(solver, floor_vertices, 4,
                                floor_triangles, 2),
               solver, "set STATIC floor");

    const KcsVec3 shell_vertices[] = {
        {-0.5f, 0.7f, -0.5f}, {0.5f, 0.7f, -0.5f},
        {0.5f, 0.7f, 0.5f}, {-0.5f, 0.7f, 0.5f}};
    const KcsTriangle shell_triangles[] = {{0, 1, 2}, {0, 2, 3}};
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    material.thickness = 0.02f;
    material.friction = 0.6f;
    require_ok(kcsSetShellMesh(solver, shell_vertices, 4,
                               shell_triangles, 2, &material),
               solver, "set contact SHELL");
    require_ok(kcsBuild(solver), solver, "build contact solver");

    for (int frame = 0; frame < 150; ++frame) {
        require_ok(kcsStep(solver, 1.0f / 60.0f), solver, "contact step");
    }
    KcsVec3 result[4];
    KcsVec3 velocity[4];
    require_ok(kcsCopyShellPositions(solver, result, 4), solver,
               "copy contact positions");
    require_ok(kcsCopyShellVelocities(solver, velocity, 4), solver,
               "copy contact velocities");
    float minimum_y = result[0].y;
    float maximum_speed = 0.0f;
    for (int i = 0; i < 4; ++i) {
        minimum_y = std::min(minimum_y, result[i].y);
        maximum_speed = std::max(maximum_speed,
            std::sqrt(velocity[i].x * velocity[i].x +
                      velocity[i].y * velocity[i].y +
                      velocity[i].z * velocity[i].z));
    }
    require(minimum_y >= material.thickness * 0.95f,
            "SHELL vertices must remain above STATIC by thickness");
    require(maximum_speed < 0.25f, "resting SHELL should not gain energy");

    KcsStepStats stats{};
    stats.struct_size = sizeof(stats);
    require_ok(kcsGetLastStepStats(solver, &stats), solver, "get step stats");
    require(stats.contact_count > 0, "resting frame must report contacts");
    kcsDestroy(solver);
}

void test_seam_thread() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.substeps = 4;
    desc.pd_iterations = 10;
    desc.pcg_iterations = 200;

    const KcsVec3 floor_vertices[] = {
        {-3.0f, 0.0f, -3.0f}, {3.0f, 0.0f, -3.0f},
        {3.0f, 0.0f, 3.0f}, {-3.0f, 0.0f, 3.0f}};
    const KcsTriangle floor_triangles[] = {{0, 2, 1}, {0, 3, 2}};

    const KcsVec3 shell_vertices[] = {
        {-0.2f, 0.02f, -0.2f}, {0.2f, 0.02f, -0.2f},
        {0.0f, 0.02f, 0.2f},
        {-0.2f, 1.0f, -0.2f}, {0.2f, 1.0f, -0.2f},
        {0.0f, 1.0f, 0.2f}};
    const KcsTriangle shell_triangles[] = {{0, 1, 2}, {3, 4, 5}};
    const float rest_length = distance(shell_vertices[0], shell_vertices[3]);

    auto seam_error = [&](float stiffness) {
        KcsSolver *solver = kcsCreate(&desc);
        require(solver != nullptr, "create seam solver");
        require_ok(kcsSetStaticMesh(solver, floor_vertices, 4,
                                    floor_triangles, 2),
                   solver, "set seam STATIC floor");
        KcsShellMaterial material;
        kcsDefaultShellMaterial(&material);
        material.thickness = 0.02f;
        require_ok(kcsSetShellMesh(solver, shell_vertices, 6,
                                   shell_triangles, 2, &material),
                   solver, "set disconnected seam SHELL");
        const KcsSeam seams[] = {{0, 3, stiffness}};
        require_ok(kcsSetShellSeams(solver, seams, 1), solver,
                   "set finite seam");
        require_ok(kcsBuild(solver), solver, "build seam solver");
        for (int frame = 0; frame < 10; ++frame) {
            require_ok(kcsStep(solver, 1.0f / 60.0f), solver, "seam step");
        }
        KcsVec3 result[6];
        require_ok(kcsCopyShellPositions(solver, result, 6), solver,
                   "copy seam positions");
        const float error = distance(result[0], result[3]);
        kcsDestroy(solver);
        return error;
    };

    const float soft_error = seam_error(100.0f);
    const float strong_error = seam_error(100000.0f);
    require(strong_error < soft_error * 0.25f,
            "higher finite seam stiffness must reduce seam strain");
    require(strong_error < rest_length * 0.05f,
            "strong finite seam must close to below five percent of its initial gap");

    // MD/HOU stores sewn boundaries as distinct vertices at the same position.
    // A zero initial distance is valid and the stitch keeps them together.
    KcsVec3 sewn_vertices[6];
    std::copy(std::begin(shell_vertices), std::end(shell_vertices),
              std::begin(sewn_vertices));
    sewn_vertices[3] = sewn_vertices[0];
    KcsSolver *sewn = kcsCreate(&desc);
    require(sewn != nullptr, "create already-sewn solver");
    require_ok(kcsSetStaticMesh(sewn, floor_vertices, 4, floor_triangles, 2),
               sewn, "set already-sewn STATIC floor");
    KcsShellMaterial sewn_material;
    kcsDefaultShellMaterial(&sewn_material);
    sewn_material.thickness = 0.02f;
    require_ok(kcsSetShellMesh(sewn, sewn_vertices, 6, shell_triangles, 2,
                               &sewn_material),
               sewn, "set already-sewn SHELL");
    const KcsSeam sewn_seam[] = {{0, 3, 100000.0f}};
    require_ok(kcsSetShellSeams(sewn, sewn_seam, 1), sewn,
               "set zero-length initial seam");
    require_ok(kcsBuild(sewn), sewn, "build zero-length initial seam");
    require_ok(kcsStep(sewn, 1.0f / 60.0f), sewn,
               "step zero-length initial seam");
    kcsDestroy(sewn);
}

void test_triangle_strain_limit() {
    const KcsVec3 wall_vertices[] = {
        {0.0f, -2.0f, -2.0f}, {0.0f, 2.0f, -2.0f},
        {0.0f, 2.0f, 2.0f}, {0.0f, -2.0f, 2.0f}};
    const KcsTriangle wall_triangles[] = {{0, 1, 2}, {0, 2, 3}};
    const KcsVec3 shell_vertices[] = {
        {-0.001f, -0.5f, 0.0f}, {0.5f, -0.5f, 0.0f},
        {0.5f, 0.5f, 0.0f}};
    const KcsTriangle shell_triangle[] = {{0, 1, 2}};

    auto run = [&](float limit, float stiffness, uint64_t *projections) {
        KcsSolverDesc desc;
        kcsDefaultSolverDesc(&desc);
        desc.gravity = {0.0f, 0.0f, 0.0f};
        desc.substeps = 4;
        desc.pd_iterations = 8;
        desc.pcg_iterations = 300;
        desc.thread_count = 2;
        KcsSolver *solver = kcsCreate(&desc);
        require(solver != nullptr, "create strain-limit solver");
        require_ok(kcsSetStaticMesh(solver, wall_vertices, 4,
                                    wall_triangles, 2),
                   solver, "set strain-limit wall");
        KcsShellMaterial material;
        kcsDefaultShellMaterial(&material);
        material.stretch_stiffness = 1.0f;
        material.bend_stiffness = 0.0f;
        material.thickness = 0.2f;
        material.strain_limit = limit;
        material.strain_limit_stiffness = stiffness;
        require_ok(kcsSetShellMesh(solver, shell_vertices, 3,
                                   shell_triangle, 1, &material),
                   solver, "set strain-limit SHELL");
        require_ok(kcsBuild(solver), solver, "build strain-limit solver");
        KcsStepStats stats{};
        for (int frame = 0; frame < 10; ++frame) {
            require_ok(kcsStep(solver, 1.0f / 24.0f), solver,
                       "strain-limit step");
            stats.struct_size = sizeof(stats);
            require_ok(kcsGetLastStepStats(solver, &stats), solver,
                       "strain-limit stats");
            *projections += stats.strain_limit_projection_count;
        }
        const float maximum = stats.maximum_principal_stretch;
        kcsDestroy(solver);
        return maximum;
    };

    uint64_t disabled_projections = 0u;
    uint64_t enabled_projections = 0u;
    const float unlimited = run(0.0f, 0.0f, &disabled_projections);
    const float limited = run(0.05f, 10000.0f, &enabled_projections);
    require(unlimited > 2.0f,
            "stress scene must visibly stretch without the limiter");
    require(limited <= 1.06f,
            "five-percent triangle strain limit must bound principal stretch");
    require(limited < unlimited * 0.5f,
            "triangle strain limit must materially reduce stretch");
    require(disabled_projections == 0u && enabled_projections > 0u,
            "strain-limit statistics must report active projections");
}

void test_invalid_mesh() {
    KcsSolver *solver = kcsCreate(nullptr);
    require(solver != nullptr, "create invalid-mesh solver");
    const KcsVec3 vertices[] = {{0, 0, 0}, {1, 0, 0}, {2, 0, 0}};
    const KcsTriangle triangle[] = {{0, 1, 2}};
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    require_ok(kcsSetShellMesh(solver, vertices, 3, triangle, 1, &material),
               solver, "accept arrays before validation");
    require(kcsBuild(solver) == KCS_ERROR_INVALID_MESH,
            "degenerate SHELL must fail at build");
    require(kcsGetLastError(solver)[0] != '\0', "mesh failure must have an error message");
    kcsDestroy(solver);
}

void test_openmp_sized_mesh() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.substeps = 1;
    desc.pd_iterations = 2;
    desc.pcg_iterations = 20;
    desc.thread_count = 4;
    KcsSolver *solver = kcsCreate(&desc);
    require(solver != nullptr, "create OpenMP-sized solver");

    constexpr uint32_t side = 66; // 4,356 vertices: exceeds parallel threshold.
    std::vector<KcsVec3> vertices;
    std::vector<KcsTriangle> triangles;
    vertices.reserve(side * side);
    for (uint32_t z = 0; z < side; ++z) {
        for (uint32_t x = 0; x < side; ++x) {
            vertices.push_back({0.02f * x, 2.0f, 0.02f * z});
        }
    }
    for (uint32_t z = 0; z + 1 < side; ++z) {
        for (uint32_t x = 0; x + 1 < side; ++x) {
            const uint32_t a = z * side + x;
            const uint32_t b = a + 1;
            const uint32_t c = a + side;
            const uint32_t d = c + 1;
            triangles.push_back({a, c, b});
            triangles.push_back({b, c, d});
        }
    }
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    require_ok(kcsSetShellMesh(solver, vertices.data(),
                               static_cast<uint32_t>(vertices.size()),
                               triangles.data(),
                               static_cast<uint32_t>(triangles.size()),
                               &material),
               solver, "set OpenMP-sized SHELL");
    require_ok(kcsBuild(solver), solver, "build OpenMP-sized solver");
    require_ok(kcsStep(solver, 1.0f / 60.0f), solver, "OpenMP-sized step");
    require_ok(kcsCopyShellPositions(solver, vertices.data(),
                                     static_cast<uint32_t>(vertices.size())),
               solver, "copy OpenMP-sized positions");
    require(vertices.front().y < 2.0f &&
            std::abs(vertices.front().y - vertices.back().y) < 1.0e-4f,
            "parallel free fall must remain finite and uniform");
    kcsDestroy(solver);
}

void test_swept_floor_contact() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.substeps = 1;
    desc.pd_iterations = 3;
    desc.gravity = {0.0f, -30.0f, 0.0f};
    KcsSolver *solver = kcsCreate(&desc);
    require(solver != nullptr, "create swept-contact solver");

    const KcsVec3 floor_vertices[] = {
        {-5, 0, -5}, {5, 0, -5}, {5, 0, 5}, {-5, 0, 5}};
    const KcsTriangle floor_triangles[] = {{0, 2, 1}, {0, 3, 2}};
    require_ok(kcsSetStaticMesh(solver, floor_vertices, 4, floor_triangles, 2),
               solver, "set swept STATIC");

    const KcsVec3 shell_vertices[] = {
        {-0.2f, 1.0f, -0.2f}, {0.2f, 1.0f, -0.2f}, {0.0f, 1.0f, 0.2f}};
    const KcsTriangle shell_triangle[] = {{0, 1, 2}};
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    material.thickness = 0.025f;
    require_ok(kcsSetShellMesh(solver, shell_vertices, 3, shell_triangle, 1,
                               &material),
               solver, "set swept SHELL");
    require_ok(kcsBuild(solver), solver, "build swept-contact solver");
    /* The unconstrained predictor ends far below y=0. A discrete endpoint-only
       query would miss the plane; the swept vertex-triangle query must catch it. */
    require_ok(kcsStep(solver, 0.5f), solver, "swept-contact step");
    KcsVec3 result[3];
    require_ok(kcsCopyShellPositions(solver, result, 3), solver,
               "copy swept-contact positions");
    for (KcsVec3 p : result) {
        require(p.y >= material.thickness * 0.95f,
                "swept contact must prevent high-speed vertex tunnelling");
    }
    kcsDestroy(solver);
}

void test_animated_static_refit() {
    KcsSolverDesc desc;
    kcsDefaultSolverDesc(&desc);
    desc.gravity = {0.0f, 0.0f, 0.0f};
    desc.substeps = 4;
    desc.pd_iterations = 4;
    KcsSolver *solver = kcsCreate(&desc);
    require(solver != nullptr, "create animated-STATIC solver");

    const KcsVec3 floor_start[] = {
        {-2.0f, -0.02f, -2.0f}, {2.0f, -0.02f, -2.0f},
        {2.0f, -0.02f, 2.0f}, {-2.0f, -0.02f, 2.0f}};
    const KcsVec3 floor_end[] = {
        {-2.0f, 0.0f, -2.0f}, {2.0f, 0.0f, -2.0f},
        {2.0f, 0.0f, 2.0f}, {-2.0f, 0.0f, 2.0f}};
    const KcsTriangle floor_triangles[] = {{0, 2, 1}, {0, 3, 2}};
    require_ok(kcsSetStaticMesh(solver, floor_start, 4, floor_triangles, 2),
               solver, "set animated STATIC start");

    const KcsVec3 shell_vertices[] = {
        {-0.2f, 0.0f, -0.2f}, {0.2f, 0.0f, -0.2f},
        {0.0f, 0.0f, 0.2f}};
    const KcsTriangle shell_triangle[] = {{0, 1, 2}};
    KcsShellMaterial material;
    kcsDefaultShellMaterial(&material);
    material.thickness = 0.02f;
    require_ok(kcsSetShellMesh(solver, shell_vertices, 3, shell_triangle, 1,
                               &material),
               solver, "set animated-STATIC SHELL");
    require_ok(kcsBuild(solver), solver, "build animated-STATIC solver");
    require_ok(kcsUpdateStaticVertices(solver, floor_end, 4), solver,
               "queue animated STATIC vertices");
    require_ok(kcsStep(solver, 1.0f / 24.0f), solver,
               "step animated STATIC");

    KcsVec3 result[3];
    require_ok(kcsCopyShellPositions(solver, result, 3), solver,
               "copy animated-STATIC positions");
    for (KcsVec3 point : result) {
        require(point.y >= material.thickness * 0.95f,
                "rising STATIC must carry SHELL to its final surface");
    }

    KcsVec3 temporarily_degenerate[] = {
        floor_end[0], floor_end[1], floor_end[0], floor_end[3]};
    require_ok(kcsUpdateStaticVertices(solver, temporarily_degenerate, 4), solver,
               "queue temporarily degenerate STATIC");
    require_ok(kcsStep(solver, 1.0f / 24.0f), solver,
               "ignore temporarily degenerate STATIC triangles");
    require_ok(kcsUpdateStaticVertices(solver, floor_end, 4), solver,
               "reactivate animated STATIC triangles");
    require_ok(kcsStep(solver, 1.0f / 24.0f), solver,
               "step reactivated STATIC triangles");
    kcsDestroy(solver);
}

} // namespace

int main() {
    require(kcsGetAbiVersion() == KCS_ABI_VERSION, "ABI version");
    require(kcsIsOpenMpEnabled() == 1, "library must be compiled with OpenMP");
    test_free_fall();
    test_static_floor_contact();
    test_seam_thread();
    test_triangle_strain_limit();
    test_invalid_mesh();
    test_openmp_sized_mesh();
    test_swept_floor_contact();
    test_animated_static_refit();
    std::puts("All Koromo solver tests passed.");
    return 0;
}
