#include "cuda_backend.hpp"
#include "solver.hpp"

#include <cuda_runtime.h>
#include <cub/device/device_reduce.cuh>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace kcs {
namespace {

constexpr float kEpsilon = 1.0e-7f;
constexpr int kBlockSize = 256;

class CudaError : public std::runtime_error {
public:
    explicit CudaError(const std::string &message) : std::runtime_error(message) {}
};

void cuda_check(cudaError_t result, const char *operation) {
    if (result == cudaSuccess) return;
    throw CudaError(std::string(operation) + ": " + cudaGetErrorString(result));
}

#define KCS_CUDA_CHECK(call) cuda_check((call), #call)

template <typename T>
class DeviceBuffer {
public:
    DeviceBuffer() = default;
    ~DeviceBuffer() { cudaFree(data_); }
    DeviceBuffer(const DeviceBuffer &) = delete;
    DeviceBuffer &operator=(const DeviceBuffer &) = delete;

    void resize(size_t count) {
        if (count == count_) return;
        KCS_CUDA_CHECK(cudaFree(data_));
        data_ = nullptr;
        count_ = 0;
        if (count != 0u) {
            KCS_CUDA_CHECK(cudaMalloc(reinterpret_cast<void **>(&data_),
                                      count * sizeof(T)));
            count_ = count;
        }
    }

    void upload(const std::vector<T> &values) {
        resize(values.size());
        if (!values.empty()) {
            KCS_CUDA_CHECK(cudaMemcpy(data_, values.data(),
                                      values.size() * sizeof(T),
                                      cudaMemcpyHostToDevice));
        }
    }

    void download(std::vector<T> &values) const {
        values.resize(count_);
        if (count_ != 0u) {
            KCS_CUDA_CHECK(cudaMemcpy(values.data(), data_, count_ * sizeof(T),
                                      cudaMemcpyDeviceToHost));
        }
    }

    void clear() {
        if (count_ != 0u) {
            KCS_CUDA_CHECK(cudaMemset(data_, 0, count_ * sizeof(T)));
        }
    }

    T *get() { return data_; }
    const T *get() const { return data_; }
    size_t size() const { return count_; }
    size_t bytes() const { return count_ * sizeof(T); }

private:
    T *data_ = nullptr;
    size_t count_ = 0;
};

int blocks_for(size_t count) {
    return static_cast<int>((count + kBlockSize - 1u) / kBlockSize);
}

__host__ __device__ Vec3 vadd(Vec3 a, Vec3 b) {
    return {a.x + b.x, a.y + b.y, a.z + b.z};
}

__host__ __device__ Vec3 vsub(Vec3 a, Vec3 b) {
    return {a.x - b.x, a.y - b.y, a.z - b.z};
}

__host__ __device__ Vec3 vmul(Vec3 a, float s) {
    return {a.x * s, a.y * s, a.z * s};
}

__host__ __device__ Vec3 vdiv(Vec3 a, float s) {
    return {a.x / s, a.y / s, a.z / s};
}

__host__ __device__ float vdot(Vec3 a, Vec3 b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}

__host__ __device__ Vec3 vcross(Vec3 a, Vec3 b) {
    return {a.y * b.z - a.z * b.y,
            a.z * b.x - a.x * b.z,
            a.x * b.y - a.y * b.x};
}

__host__ __device__ float vlength_squared(Vec3 a) { return vdot(a, a); }
__host__ __device__ float vlength(Vec3 a) { return sqrtf(vlength_squared(a)); }

__device__ Vec3 vmin(Vec3 a, Vec3 b) {
    return {fminf(a.x, b.x), fminf(a.y, b.y), fminf(a.z, b.z)};
}

__device__ Vec3 vmax(Vec3 a, Vec3 b) {
    return {fmaxf(a.x, b.x), fmaxf(a.y, b.y), fmaxf(a.z, b.z)};
}

__device__ Aabb empty_aabb_device() {
    const float inf = __int_as_float(0x7f800000);
    return {{inf, inf, inf}, {-inf, -inf, -inf}};
}

__device__ void grow_device(Aabb &a, Vec3 p) {
    a.lo = vmin(a.lo, p);
    a.hi = vmax(a.hi, p);
}

__device__ void grow_device(Aabb &a, const Aabb &b) {
    a.lo = vmin(a.lo, b.lo);
    a.hi = vmax(a.hi, b.hi);
}

__device__ float aabb_distance_squared_device(const Aabb &a, Vec3 p) {
    const float dx = fmaxf(fmaxf(a.lo.x - p.x, 0.0f), p.x - a.hi.x);
    const float dy = fmaxf(fmaxf(a.lo.y - p.y, 0.0f), p.y - a.hi.y);
    const float dz = fmaxf(fmaxf(a.lo.z - p.z, 0.0f), p.z - a.hi.z);
    return dx * dx + dy * dy + dz * dz;
}

__device__ bool segment_aabb_device(Vec3 p0, Vec3 p1, Aabb box,
                                    float padding, float max_time) {
    box.lo.x -= padding;
    box.lo.y -= padding;
    box.lo.z -= padding;
    box.hi.x += padding;
    box.hi.y += padding;
    box.hi.z += padding;
    const Vec3 d = vsub(p1, p0);
    const float origin[3] = {p0.x, p0.y, p0.z};
    const float delta[3] = {d.x, d.y, d.z};
    const float lo[3] = {box.lo.x, box.lo.y, box.lo.z};
    const float hi[3] = {box.hi.x, box.hi.y, box.hi.z};
    float tmin = 0.0f;
    float tmax = max_time;
    for (int axis = 0; axis < 3; ++axis) {
        if (fabsf(delta[axis]) < kEpsilon) {
            if (origin[axis] < lo[axis] || origin[axis] > hi[axis]) return false;
            continue;
        }
        const float inverse = 1.0f / delta[axis];
        float a = (lo[axis] - origin[axis]) * inverse;
        float b = (hi[axis] - origin[axis]) * inverse;
        if (a > b) {
            const float temporary = a;
            a = b;
            b = temporary;
        }
        tmin = fmaxf(tmin, a);
        tmax = fminf(tmax, b);
        if (tmin > tmax) return false;
    }
    return true;
}

__device__ Vec3 closest_point_triangle_device(Vec3 p, Vec3 a, Vec3 b, Vec3 c) {
    const Vec3 ab = vsub(b, a);
    const Vec3 ac = vsub(c, a);
    const Vec3 ap = vsub(p, a);
    const float d1 = vdot(ab, ap);
    const float d2 = vdot(ac, ap);
    if (d1 <= 0.0f && d2 <= 0.0f) return a;
    const Vec3 bp = vsub(p, b);
    const float d3 = vdot(ab, bp);
    const float d4 = vdot(ac, bp);
    if (d3 >= 0.0f && d4 <= d3) return b;
    const float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0f && d1 >= 0.0f && d3 <= 0.0f) {
        return vadd(a, vmul(ab, d1 / (d1 - d3)));
    }
    const Vec3 cp = vsub(p, c);
    const float d5 = vdot(ab, cp);
    const float d6 = vdot(ac, cp);
    if (d6 >= 0.0f && d5 <= d6) return c;
    const float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0f && d2 >= 0.0f && d6 <= 0.0f) {
        return vadd(a, vmul(ac, d2 / (d2 - d6)));
    }
    const float va = d3 * d6 - d5 * d4;
    if (va <= 0.0f && (d4 - d3) >= 0.0f && (d5 - d6) >= 0.0f) {
        const Vec3 bc = vsub(c, b);
        const float w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        return vadd(b, vmul(bc, w));
    }
    const float denominator = 1.0f / (va + vb + vc);
    return vadd(a, vadd(vmul(ab, vb * denominator),
                         vmul(ac, vc * denominator)));
}

__device__ void triangle_barycentric_device(Vec3 p, Vec3 a, Vec3 b, Vec3 c,
                                             float weights[3]) {
    const Vec3 ab = vsub(b, a);
    const Vec3 ac = vsub(c, a);
    const Vec3 ap = vsub(p, a);
    const float d00 = vdot(ab, ab);
    const float d01 = vdot(ab, ac);
    const float d11 = vdot(ac, ac);
    const float d20 = vdot(ap, ab);
    const float d21 = vdot(ap, ac);
    const float denominator = d00 * d11 - d01 * d01;
    if (!(fabsf(denominator) > kEpsilon * kEpsilon)) {
        weights[0] = 1.0f;
        weights[1] = 0.0f;
        weights[2] = 0.0f;
        return;
    }
    const float inverse = 1.0f / denominator;
    weights[1] = (d11 * d20 - d01 * d21) * inverse;
    weights[2] = (d00 * d21 - d01 * d20) * inverse;
    weights[0] = 1.0f - weights[1] - weights[2];
}

__device__ bool segment_triangle_device(Vec3 p0, Vec3 p1, Vec3 a, Vec3 b,
                                         Vec3 c, float &time) {
    const Vec3 d = vsub(p1, p0);
    const Vec3 e1 = vsub(b, a);
    const Vec3 e2 = vsub(c, a);
    const Vec3 p = vcross(d, e2);
    const float determinant = vdot(e1, p);
    if (fabsf(determinant) < kEpsilon) return false;
    const float inverse = 1.0f / determinant;
    const Vec3 t = vsub(p0, a);
    const float u = vdot(t, p) * inverse;
    if (u < -kEpsilon || u > 1.0f + kEpsilon) return false;
    const Vec3 q = vcross(t, e1);
    const float v = vdot(d, q) * inverse;
    if (v < -kEpsilon || u + v > 1.0f + kEpsilon) return false;
    const float hit_time = vdot(e2, q) * inverse;
    if (hit_time < kEpsilon || hit_time > time) return false;
    time = hit_time;
    return true;
}

struct DeviceClosestHit {
    bool hit = false;
    float distance_squared = 0.0f;
    Vec3 point{};
    Vec3 normal{};
    uint32_t vertex[3]{};
    float barycentric[3]{};
};

struct DeviceSegmentHit {
    bool hit = false;
    float time = 1.0f;
    Vec3 point{};
    Vec3 normal{};
};

__device__ DeviceClosestHit closest_within_device(
    Vec3 point, float radius, const Vec3 *vertices,
    const StaticTriangle *triangles, const uint32_t *order,
    const BvhNode *nodes, uint32_t node_count) {
    DeviceClosestHit result;
    result.distance_squared = radius * radius;
    if (node_count == 0u) return result;
    uint32_t stack[128];
    uint32_t stack_size = 0u;
    stack[stack_size++] = 0u;
    while (stack_size != 0u) {
        const uint32_t node_index = stack[--stack_size];
        const BvhNode &node = nodes[node_index];
        if (aabb_distance_squared_device(node.bounds, point) >
            result.distance_squared) continue;
        if (node.count != 0u) {
            for (uint32_t i = node.first; i < node.first + node.count; ++i) {
                const StaticTriangle &triangle = triangles[order[i]];
                if (!triangle.active) continue;
                const Vec3 a = vertices[triangle.i[0]];
                const Vec3 b = vertices[triangle.i[1]];
                const Vec3 c = vertices[triangle.i[2]];
                const Vec3 q = closest_point_triangle_device(point, a, b, c);
                const float distance_squared = vlength_squared(vsub(point, q));
                if (distance_squared <= result.distance_squared) {
                    result.hit = true;
                    result.distance_squared = distance_squared;
                    result.point = q;
                    result.normal = triangle.normal;
                    result.vertex[0] = triangle.i[0];
                    result.vertex[1] = triangle.i[1];
                    result.vertex[2] = triangle.i[2];
                    triangle_barycentric_device(q, a, b, c,
                                                result.barycentric);
                }
            }
        } else if (stack_size + 2u <= 128u) {
            stack[stack_size++] = node.left;
            stack[stack_size++] = node.right;
        }
    }
    return result;
}

__device__ DeviceSegmentHit first_segment_hit_device(
    Vec3 from, Vec3 to, float padding, const Vec3 *vertices,
    const StaticTriangle *triangles, const uint32_t *order,
    const BvhNode *nodes, uint32_t node_count) {
    DeviceSegmentHit result;
    if (node_count == 0u ||
        vlength_squared(vsub(to, from)) <= kEpsilon * kEpsilon) return result;
    uint32_t stack[128];
    uint32_t stack_size = 0u;
    stack[stack_size++] = 0u;
    while (stack_size != 0u) {
        const uint32_t node_index = stack[--stack_size];
        const BvhNode &node = nodes[node_index];
        if (!segment_aabb_device(from, to, node.bounds, padding, result.time))
            continue;
        if (node.count != 0u) {
            for (uint32_t i = node.first; i < node.first + node.count; ++i) {
                const StaticTriangle &triangle = triangles[order[i]];
                if (!triangle.active) continue;
                float hit_time = result.time;
                if (segment_triangle_device(
                        from, to, vertices[triangle.i[0]],
                        vertices[triangle.i[1]], vertices[triangle.i[2]],
                        hit_time)) {
                    result.hit = true;
                    result.time = hit_time;
                    result.point = vadd(from, vmul(vsub(to, from), hit_time));
                    result.normal = triangle.normal;
                }
            }
        } else if (stack_size + 2u <= 128u) {
            stack[stack_size++] = node.left;
            stack[stack_size++] = node.right;
        }
    }
    return result;
}

__global__ void interpolate_static_kernel(const Vec3 *current,
                                           const Vec3 *target, Vec3 *output,
                                           size_t count, float alpha) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        output[i] = vadd(vmul(current[i], 1.0f - alpha),
                         vmul(target[i], alpha));
    }
}

__global__ void refit_triangles_kernel(const Vec3 *vertices,
                                        StaticTriangle *triangles,
                                        size_t triangle_count) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= triangle_count) return;
    StaticTriangle &triangle = triangles[i];
    const Vec3 a = vertices[triangle.i[0]];
    const Vec3 b = vertices[triangle.i[1]];
    const Vec3 c = vertices[triangle.i[2]];
    const Vec3 normal_raw = vcross(vsub(b, a), vsub(c, a));
    const float normal_length = vlength(normal_raw);
    if (!(normal_length > kEpsilon) || !isfinite(normal_length)) {
        triangle.active = false;
        triangle.bounds = empty_aabb_device();
        return;
    }
    triangle.active = true;
    triangle.normal = vdiv(normal_raw, normal_length);
    triangle.centroid = vdiv(vadd(vadd(a, b), c), 3.0f);
    triangle.bounds = empty_aabb_device();
    grow_device(triangle.bounds, a);
    grow_device(triangle.bounds, b);
    grow_device(triangle.bounds, c);
}

__global__ void refit_nodes_kernel(const StaticTriangle *triangles,
                                    const uint32_t *order, BvhNode *nodes,
                                    size_t node_count) {
    if (blockIdx.x != 0 || threadIdx.x != 0) return;
    for (size_t reverse = node_count; reverse-- > 0u;) {
        BvhNode &node = nodes[reverse];
        Aabb bounds = empty_aabb_device();
        if (node.count != 0u) {
            for (uint32_t i = node.first; i < node.first + node.count; ++i) {
                grow_device(bounds, triangles[order[i]].bounds);
            }
        } else {
            grow_device(bounds, nodes[node.left].bounds);
            grow_device(bounds, nodes[node.right].bounds);
        }
        node.bounds = bounds;
    }
}

__global__ void predict_kernel(Vec3 *velocities, const Vec3 *positions,
                               Vec3 *predicted, Vec3 *iterate, size_t count,
                               float damping, Vec3 gravity, float h,
                               float h_squared) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    velocities[i] = vmul(velocities[i], damping);
    predicted[i] = vadd(vadd(positions[i], vmul(velocities[i], h)),
                        vmul(gravity, h_squared));
    iterate[i] = predicted[i];
}

__global__ void project_constraints_kernel(
    const Constraint *constraints, size_t constraint_count,
    const Vec3 *iterate, const Vec3 *rest_positions, Vec3 *projection) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= constraint_count) return;
    const Constraint &constraint = constraints[i];
    const Vec3 delta = vsub(iterate[constraint.a], iterate[constraint.b]);
    const float current_length = vlength(delta);
    if (current_length > kEpsilon) {
        projection[i] = vmul(
            delta, constraint.weight * constraint.rest_length / current_length);
    } else {
        const Vec3 rest_delta =
            vsub(rest_positions[constraint.a], rest_positions[constraint.b]);
        projection[i] = vmul(
            rest_delta,
            constraint.weight * constraint.rest_length /
                fmaxf(vlength(rest_delta), kEpsilon));
    }
}

__device__ float project_triangle_strain_device(
    Vec3 f0, Vec3 f1, float minimum_stretch, float maximum_stretch,
    Vec3 &p0, Vec3 &p1, bool &limited) {
    const float c00 = vdot(f0, f0);
    const float c01 = vdot(f0, f1);
    const float c11 = vdot(f1, f1);
    const float discriminant = sqrtf(fmaxf(
        (c00 - c11) * (c00 - c11) + 4.0f * c01 * c01, 0.0f));
    const float lambda0 = fmaxf(0.5f * (c00 + c11 + discriminant), 0.0f);
    const float lambda1 = fmaxf(0.5f * (c00 + c11 - discriminant), 0.0f);
    const float sigma0 = sqrtf(lambda0);
    const float sigma1 = sqrtf(lambda1);
    limited = sigma0 > maximum_stretch || sigma1 < minimum_stretch;
    if (!limited) {
        p0 = f0;
        p1 = f1;
        return sigma0;
    }
    float vx = 1.0f;
    float vy = 0.0f;
    if (fabsf(c01) > kEpsilon) {
        vx = c01;
        vy = lambda0 - c00;
        const float inverse_length =
            1.0f / sqrtf(fmaxf(vx * vx + vy * vy, kEpsilon));
        vx *= inverse_length;
        vy *= inverse_length;
    } else if (c11 > c00) {
        vx = 0.0f;
        vy = 1.0f;
    }
    const float projected0 = fminf(fmaxf(sigma0, minimum_stretch),
                                    maximum_stretch);
    const float projected1 = fminf(fmaxf(sigma1, minimum_stretch),
                                    maximum_stretch);
    const float scale0 = sigma0 > kEpsilon ? projected0 / sigma0 : 1.0f;
    const float scale1 = sigma1 > kEpsilon ? projected1 / sigma1 : 1.0f;
    const float wx = -vy;
    const float wy = vx;
    const float q00 = scale0 * vx * vx + scale1 * wx * wx;
    const float q01 = scale0 * vx * vy + scale1 * wx * wy;
    const float q11 = scale0 * vy * vy + scale1 * wy * wy;
    p0 = vadd(vmul(f0, q00), vmul(f1, q01));
    p1 = vadd(vmul(f0, q01), vmul(f1, q11));
    return sigma0;
}

__global__ void project_strain_kernel(
    const StrainConstraint *constraints, size_t constraint_count,
    const Vec3 *positions, const StrainProjection *dual,
    StrainProjection *projection, unsigned long long *limited_count) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= constraint_count) return;
    const StrainConstraint &constraint = constraints[i];
    Vec3 f0{};
    Vec3 f1{};
    for (int local = 0; local < 3; ++local) {
        const Vec3 position = positions[constraint.vertex[local]];
        f0 = vadd(f0, vmul(position, constraint.gradient[local][0]));
        f1 = vadd(f1, vmul(position, constraint.gradient[local][1]));
    }
    f0 = vadd(f0, dual[i].column[0]);
    f1 = vadd(f1, dual[i].column[1]);
    bool limited = false;
    project_triangle_strain_device(
        f0, f1, constraint.minimum_stretch, constraint.maximum_stretch,
        projection[i].column[0], projection[i].column[1], limited);
    if (limited) atomicAdd(limited_count, 1ull);
}

__global__ void update_strain_duals_kernel(
    const StrainConstraint *constraints, size_t constraint_count,
    const Vec3 *positions, const StrainProjection *projection,
    StrainProjection *dual) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= constraint_count) return;
    const StrainConstraint &constraint = constraints[i];
    Vec3 f0{};
    Vec3 f1{};
    for (int local = 0; local < 3; ++local) {
        const Vec3 position = positions[constraint.vertex[local]];
        f0 = vadd(f0, vmul(position, constraint.gradient[local][0]));
        f1 = vadd(f1, vmul(position, constraint.gradient[local][1]));
    }
    dual[i].column[0] =
        vadd(dual[i].column[0], vsub(f0, projection[i].column[0]));
    dual[i].column[1] =
        vadd(dual[i].column[1], vsub(f1, projection[i].column[1]));
}

__global__ void resolve_collisions_kernel(
    const Vec3 *from, Vec3 *positions, Vec3 *contact_normals,
    uint8_t *contacted, size_t vertex_count, float thickness, float skin,
    float motion_radius, const Vec3 *static_vertices,
    const Vec3 *previous_static_vertices, const StaticTriangle *triangles,
    const uint32_t *order, const BvhNode *nodes, uint32_t node_count,
    unsigned long long *contacts) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= vertex_count || node_count == 0u) return;
    Vec3 point = positions[i];
    Vec3 normal{};
    bool has_contact = false;

    const DeviceSegmentHit crossing = first_segment_hit_device(
        from[i], point, thickness, static_vertices, triangles, order, nodes,
        node_count);
    if (crossing.hit) {
        float side = vdot(vsub(from[i], crossing.point), crossing.normal);
        if (fabsf(side) < kEpsilon) {
            side = -vdot(vsub(point, from[i]), crossing.normal);
        }
        normal = side >= 0.0f ? crossing.normal : vmul(crossing.normal, -1.0f);
        point = vadd(crossing.point, vmul(normal, thickness + skin));
        has_contact = true;
    }

    const DeviceClosestHit nearest = closest_within_device(
        point, thickness + skin, static_vertices, triangles, order, nodes,
        node_count);
    if (nearest.hit && nearest.distance_squared < thickness * thickness) {
        const Vec3 offset = vsub(point, nearest.point);
        const float distance = vlength(offset);
        Vec3 push_normal{};
        if (distance > kEpsilon) {
            push_normal = vdiv(offset, distance);
        } else {
            const float side = vdot(vsub(from[i], nearest.point), nearest.normal);
            push_normal =
                side >= 0.0f ? nearest.normal : vmul(nearest.normal, -1.0f);
        }
        point = vadd(point,
                     vmul(push_normal, thickness + skin - distance));
        normal = push_normal;
        has_contact = true;
    }

    if (motion_radius > kEpsilon) {
        const DeviceClosestHit moving = closest_within_device(
            point, thickness + skin + motion_radius, static_vertices,
            triangles, order, nodes, node_count);
        if (moving.hit) {
            const Vec3 previous_a = previous_static_vertices[moving.vertex[0]];
            const Vec3 previous_b = previous_static_vertices[moving.vertex[1]];
            const Vec3 previous_c = previous_static_vertices[moving.vertex[2]];
            const Vec3 previous_point = vadd(
                vadd(vmul(previous_a, moving.barycentric[0]),
                     vmul(previous_b, moving.barycentric[1])),
                vmul(previous_c, moving.barycentric[2]));
            Vec3 previous_normal =
                vcross(vsub(previous_b, previous_a), vsub(previous_c, previous_a));
            const float previous_normal_length = vlength(previous_normal);
            if (previous_normal_length > kEpsilon) {
                previous_normal = vdiv(previous_normal, previous_normal_length);
                if (vdot(previous_normal, moving.normal) < 0.0f) {
                    previous_normal = vmul(previous_normal, -1.0f);
                }
                const float previous_distance =
                    vdot(vsub(from[i], previous_point), previous_normal);
                const float current_distance =
                    vdot(vsub(point, moving.point), moving.normal);
                const float surface_motion =
                    vdot(vsub(moving.point, previous_point), moving.normal);
                float side = previous_distance >= 0.0f ? 1.0f : -1.0f;
                if (fabsf(previous_distance) < kEpsilon &&
                    fabsf(surface_motion) > kEpsilon) {
                    side = surface_motion >= 0.0f ? 1.0f : -1.0f;
                }
                const float previous_on_side = side * previous_distance;
                const float current_on_side = side * current_distance;
                const bool approached =
                    current_on_side < previous_on_side - kEpsilon;
                if (approached && current_on_side < thickness + skin) {
                    normal = vmul(moving.normal, side);
                    point = vadd(moving.point, vmul(normal, thickness + skin));
                    has_contact = true;
                }
            }
        }
    }

    positions[i] = point;
    if (has_contact) {
        contact_normals[i] = normal;
        contacted[i] = 1u;
        atomicAdd(contacts, 1ull);
    }
}

__global__ void build_rhs_kernel(
    const Vec3 *predicted, const float *masses, float inverse_h_squared,
    const uint32_t *incidence_offsets, const Incidence *incidence,
    const Vec3 *projection, const uint32_t *strain_incidence_offsets,
    const StrainIncidence *strain_incidence,
    const StrainConstraint *strain_constraints,
    const StrainProjection *strain_projection,
    const StrainProjection *strain_dual, const uint8_t *active_contacts,
    const Vec3 *contact_targets, float contact_weight, Vec3 *rhs,
    uint8_t *contacted, size_t vertex_count) {
    const size_t vertex = blockIdx.x * blockDim.x + threadIdx.x;
    if (vertex >= vertex_count) return;
    Vec3 value = vmul(predicted[vertex], masses[vertex] * inverse_h_squared);
    for (uint32_t k = incidence_offsets[vertex];
         k < incidence_offsets[vertex + 1u]; ++k) {
        const Incidence &item = incidence[k];
        value = vadd(value, vmul(projection[item.constraint], item.sign));
    }
    for (uint32_t k = strain_incidence_offsets[vertex];
         k < strain_incidence_offsets[vertex + 1u]; ++k) {
        const StrainIncidence &item = strain_incidence[k];
        const StrainConstraint &constraint =
            strain_constraints[item.constraint];
        const StrainProjection &projected =
            strain_projection[item.constraint];
        const float gx = constraint.gradient[item.local_vertex][0];
        const float gy = constraint.gradient[item.local_vertex][1];
        value = vadd(
            value,
            vmul(vadd(vmul(vsub(projected.column[0],
                                strain_dual[item.constraint].column[0]), gx),
                       vmul(vsub(projected.column[1],
                                strain_dual[item.constraint].column[1]), gy)),
                 constraint.weighted_area));
    }
    if (active_contacts[vertex]) {
        value = vadd(value, vmul(contact_targets[vertex], contact_weight));
        contacted[vertex] = 1u;
    }
    rhs[vertex] = value;
}

__global__ void apply_system_kernel(
    const Vec3 *x, const float *masses, float inverse_h_squared,
    const uint32_t *incidence_offsets, const Incidence *incidence,
    const Constraint *constraints, const uint32_t *strain_incidence_offsets,
    const StrainIncidence *strain_incidence,
    const StrainConstraint *strain_constraints,
    const uint8_t *active_contacts, float contact_weight, Vec3 *output,
    size_t vertex_count) {
    const size_t vertex = blockIdx.x * blockDim.x + threadIdx.x;
    if (vertex >= vertex_count) return;
    Vec3 value = vmul(x[vertex], masses[vertex] * inverse_h_squared);
    for (uint32_t k = incidence_offsets[vertex];
         k < incidence_offsets[vertex + 1u]; ++k) {
        const Incidence &item = incidence[k];
        value = vadd(value,
                     vmul(vsub(x[vertex], x[item.other]),
                          constraints[item.constraint].weight));
    }
    for (uint32_t k = strain_incidence_offsets[vertex];
         k < strain_incidence_offsets[vertex + 1u]; ++k) {
        const StrainIncidence &item = strain_incidence[k];
        const StrainConstraint &constraint =
            strain_constraints[item.constraint];
        for (uint32_t other = 0; other < 3u; ++other) {
            value = vadd(
                value,
                vmul(x[constraint.vertex[other]],
                     constraint.system[item.local_vertex][other]));
        }
    }
    if (active_contacts[vertex]) {
        value = vadd(value, vmul(x[vertex], contact_weight));
    }
    output[vertex] = value;
}

__global__ void diagonal_residual_kernel(
    const Vec3 *rhs, const Vec3 *ap, const float *masses,
    float inverse_h_squared, const uint32_t *incidence_offsets,
    const Incidence *incidence, const Constraint *constraints,
    const uint32_t *strain_incidence_offsets,
    const StrainIncidence *strain_incidence,
    const StrainConstraint *strain_constraints,
    const uint8_t *active_contacts, float contact_weight, float *diagonal,
    Vec3 *residual, size_t vertex_count) {
    const size_t vertex = blockIdx.x * blockDim.x + threadIdx.x;
    if (vertex >= vertex_count) return;
    float value = masses[vertex] * inverse_h_squared;
    for (uint32_t k = incidence_offsets[vertex];
         k < incidence_offsets[vertex + 1u]; ++k) {
        value += constraints[incidence[k].constraint].weight;
    }
    for (uint32_t k = strain_incidence_offsets[vertex];
         k < strain_incidence_offsets[vertex + 1u]; ++k) {
        const StrainIncidence &item = strain_incidence[k];
        value += strain_constraints[item.constraint]
                     .system[item.local_vertex][item.local_vertex];
    }
    if (active_contacts[vertex]) value += contact_weight;
    diagonal[vertex] = value;
    residual[vertex] = vsub(rhs[vertex], ap[vertex]);
}

__global__ void colored_preconditioner_kernel(
    const Vec3 *residual, const float *diagonal,
    const uint32_t *incidence_offsets, const Incidence *incidence,
    const Constraint *constraints, const uint32_t *colors,
    const uint32_t *vertices, uint32_t color, size_t color_vertex_count,
    bool forward, Vec3 *output) {
    const size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= color_vertex_count) return;
    const uint32_t vertex = vertices[index];
    Vec3 value = forward ? residual[vertex]
                         : vmul(output[vertex], diagonal[vertex]);
    for (uint32_t k = incidence_offsets[vertex];
         k < incidence_offsets[vertex + 1u]; ++k) {
        const Incidence &item = incidence[k];
        const bool dependency =
            forward ? colors[item.other] < color : colors[item.other] > color;
        if (dependency) {
            value = vadd(value,
                         vmul(output[item.other],
                              constraints[item.constraint].weight));
        }
    }
    output[vertex] = vdiv(value, diagonal[vertex]);
}

__global__ void dot_values_kernel(const Vec3 *a, const Vec3 *b, double *values,
                                  size_t count) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        values[i] = static_cast<double>(a[i].x) * b[i].x +
                    static_cast<double>(a[i].y) * b[i].y +
                    static_cast<double>(a[i].z) * b[i].z;
    }
}

__global__ void update_solution_kernel(Vec3 *x, Vec3 *residual,
                                       const Vec3 *direction,
                                       const Vec3 *ap, float alpha,
                                       size_t count) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        x[i] = vadd(x[i], vmul(direction[i], alpha));
        residual[i] = vsub(residual[i], vmul(ap[i], alpha));
    }
}

__global__ void update_direction_kernel(Vec3 *direction,
                                        const Vec3 *preconditioned,
                                        float beta, size_t count) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        direction[i] = vadd(preconditioned[i], vmul(direction[i], beta));
    }
}

__global__ void finalize_kernel(const Vec3 *iterate, const Vec3 *substep_start,
                                Vec3 *positions, Vec3 *velocities,
                                const Vec3 *contact_normals,
                                const uint8_t *contacted, size_t count,
                                float inverse_h, float restitution,
                                float friction) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    Vec3 velocity = vmul(vsub(iterate[i], substep_start[i]), inverse_h);
    if (contacted[i]) {
        const Vec3 normal = contact_normals[i];
        const float normal_velocity = vdot(velocity, normal);
        if (normal_velocity < 0.0f) {
            velocity = vsub(
                velocity,
                vmul(normal, (1.0f + restitution) * normal_velocity));
        }
        const float normal_speed = vdot(velocity, normal);
        const Vec3 tangent = vsub(velocity, vmul(normal, normal_speed));
        velocity = vsub(velocity, vmul(tangent, friction));
    }
    positions[i] = iterate[i];
    velocities[i] = velocity;
}

__global__ void finite_state_kernel(const Vec3 *positions,
                                    const Vec3 *velocities, size_t count,
                                    int *valid) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count &&
        (!isfinite(positions[i].x) || !isfinite(positions[i].y) ||
         !isfinite(positions[i].z) || !isfinite(velocities[i].x) ||
         !isfinite(velocities[i].y) || !isfinite(velocities[i].z))) {
        atomicExch(valid, 0);
    }
}

__global__ void maximum_stretch_kernel(
    const StrainConstraint *constraints, size_t constraint_count,
    const Vec3 *positions, int *maximum_bits) {
    const size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= constraint_count) return;
    const StrainConstraint &constraint = constraints[i];
    Vec3 f0{};
    Vec3 f1{};
    for (int local = 0; local < 3; ++local) {
        const Vec3 position = positions[constraint.vertex[local]];
        f0 = vadd(f0, vmul(position, constraint.gradient[local][0]));
        f1 = vadd(f1, vmul(position, constraint.gradient[local][1]));
    }
    const float c00 = vdot(f0, f0);
    const float c01 = vdot(f0, f1);
    const float c11 = vdot(f1, f1);
    const float discriminant = sqrtf(fmaxf(
        (c00 - c11) * (c00 - c11) + 4.0f * c01 * c01, 0.0f));
    const float maximum =
        sqrtf(fmaxf(0.5f * (c00 + c11 + discriminant), 0.0f));
    if (isfinite(maximum) && maximum >= 0.0f) {
        atomicMax(maximum_bits, __float_as_int(maximum));
    }
}

} // namespace

struct CudaBackend::Impl {
    int device = 0;
    size_t vertex_count = 0;
    size_t constraint_count = 0;
    size_t strain_constraint_count = 0;
    size_t static_vertex_count = 0;
    size_t static_triangle_count = 0;
    size_t bvh_node_count = 0;
    bool strain_enabled = false;
    bool fallback_requested = false;
    uint32_t preconditioner_color_count = 0u;
    std::vector<uint32_t> preconditioner_offsets_host;

    DeviceBuffer<Vec3> static_vertices;
    DeviceBuffer<Vec3> static_target_vertices;
    DeviceBuffer<Vec3> static_substep_vertices;
    DeviceBuffer<Vec3> static_previous_vertices;
    DeviceBuffer<StaticTriangle> static_triangles;
    DeviceBuffer<uint32_t> bvh_order;
    DeviceBuffer<BvhNode> bvh_nodes;

    DeviceBuffer<Vec3> rest_positions;
    DeviceBuffer<Vec3> positions;
    DeviceBuffer<Vec3> velocities;
    DeviceBuffer<float> masses;
    DeviceBuffer<Constraint> constraints;
    DeviceBuffer<uint32_t> incidence_offsets;
    DeviceBuffer<Incidence> incidence;
    DeviceBuffer<StrainConstraint> strain_constraints;
    DeviceBuffer<uint32_t> strain_incidence_offsets;
    DeviceBuffer<StrainIncidence> strain_incidence;

    DeviceBuffer<Vec3> projection;
    DeviceBuffer<StrainProjection> strain_projection;
    DeviceBuffer<StrainProjection> strain_dual;
    DeviceBuffer<Vec3> rhs;
    DeviceBuffer<Vec3> pcg_r;
    DeviceBuffer<Vec3> pcg_z;
    DeviceBuffer<Vec3> pcg_p;
    DeviceBuffer<Vec3> pcg_ap;
    DeviceBuffer<float> pcg_diagonal;
    DeviceBuffer<Vec3> substep_start;
    DeviceBuffer<Vec3> predicted;
    DeviceBuffer<Vec3> iterate;
    DeviceBuffer<Vec3> contact_targets;
    DeviceBuffer<Vec3> contact_normals;
    DeviceBuffer<uint8_t> contacted;
    DeviceBuffer<uint8_t> active_contacts;

    DeviceBuffer<double> dot_values;
    DeviceBuffer<double> dot_result;
    DeviceBuffer<unsigned char> reduce_temporary;
    DeviceBuffer<unsigned long long> contacts;
    DeviceBuffer<unsigned long long> limited;
    DeviceBuffer<int> finite_valid;
    DeviceBuffer<int> maximum_stretch_bits;
    DeviceBuffer<uint32_t> preconditioner_colors;
    DeviceBuffer<uint32_t> preconditioner_vertices;
    void initialize(Solver &solver) {
        vertex_count = solver.positions_.size();
        constraint_count = solver.constraints_.size();
        strain_constraint_count = solver.strain_constraints_.size();
        strain_enabled = !solver.strain_incidence_.empty();
        static_vertex_count = solver.static_vertices_.size();
        static_triangle_count = solver.static_bvh_.triangles_.size();
        bvh_node_count = solver.static_bvh_.nodes_.size();

        static_vertices.upload(solver.static_vertices_);
        static_target_vertices.upload(solver.static_target_vertices_);
        static_substep_vertices.resize(static_vertex_count);
        static_previous_vertices.resize(static_vertex_count);
        static_triangles.upload(solver.static_bvh_.triangles_);
        bvh_order.upload(solver.static_bvh_.order_);
        bvh_nodes.upload(solver.static_bvh_.nodes_);
        rest_positions.upload(solver.rest_positions_);
        positions.upload(solver.positions_);
        velocities.upload(solver.velocities_);
        masses.upload(solver.masses_);
        constraints.upload(solver.constraints_);
        incidence_offsets.upload(solver.incidence_offsets_);
        incidence.upload(solver.incidence_);
        strain_constraints.upload(solver.strain_constraints_);
        strain_incidence_offsets.upload(solver.strain_incidence_offsets_);
        strain_incidence.upload(solver.strain_incidence_);

        projection.resize(constraint_count);
        strain_projection.resize(strain_constraint_count);
        strain_dual.resize(strain_constraint_count);
        rhs.resize(vertex_count);
        pcg_r.resize(vertex_count);
        pcg_z.resize(vertex_count);
        pcg_p.resize(vertex_count);
        pcg_ap.resize(vertex_count);
        pcg_diagonal.resize(vertex_count);
        substep_start.resize(vertex_count);
        predicted.resize(vertex_count);
        iterate.resize(vertex_count);
        contact_targets.resize(vertex_count);
        contact_normals.resize(vertex_count);
        contacted.resize(vertex_count);
        active_contacts.resize(vertex_count);
        dot_values.resize(vertex_count);
        dot_result.resize(1u);
        contacts.resize(1u);
        limited.resize(1u);
        finite_valid.resize(1u);
        maximum_stretch_bits.resize(1u);

        std::vector<uint32_t> colors_host(vertex_count, UINT32_MAX);
        std::vector<uint32_t> used_colors;
        for (uint32_t vertex = 0u; vertex < vertex_count; ++vertex) {
            used_colors.assign(preconditioner_color_count, 0u);
            for (uint32_t k = solver.incidence_offsets_[vertex];
                 k < solver.incidence_offsets_[vertex + 1u]; ++k) {
                const uint32_t other = solver.incidence_[k].other;
                if (other < vertex && colors_host[other] != UINT32_MAX) {
                    used_colors[colors_host[other]] = 1u;
                }
            }
            uint32_t color = 0u;
            while (color < used_colors.size() && used_colors[color]) ++color;
            if (color == preconditioner_color_count) {
                ++preconditioner_color_count;
            }
            colors_host[vertex] = color;
        }
        preconditioner_offsets_host.assign(
            static_cast<size_t>(preconditioner_color_count) + 1u, 0u);
        for (uint32_t color : colors_host) {
            ++preconditioner_offsets_host[color + 1u];
        }
        for (size_t i = 1; i < preconditioner_offsets_host.size(); ++i) {
            preconditioner_offsets_host[i] += preconditioner_offsets_host[i - 1u];
        }
        std::vector<uint32_t> vertices_by_color(vertex_count);
        std::vector<uint32_t> color_cursor = preconditioner_offsets_host;
        for (uint32_t vertex = 0u; vertex < vertex_count; ++vertex) {
            vertices_by_color[color_cursor[colors_host[vertex]]++] = vertex;
        }
        preconditioner_colors.upload(colors_host);
        preconditioner_vertices.upload(vertices_by_color);

        size_t temporary_bytes = 0u;
        KCS_CUDA_CHECK(cub::DeviceReduce::Sum(
            nullptr, temporary_bytes, dot_values.get(), dot_result.get(),
            vertex_count));
        reduce_temporary.resize(temporary_bytes);
    }

    double dot(const Vec3 *a, const Vec3 *b) {
        dot_values_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
            a, b, dot_values.get(), vertex_count);
        size_t temporary_bytes = reduce_temporary.size();
        KCS_CUDA_CHECK(cub::DeviceReduce::Sum(
            reduce_temporary.get(), temporary_bytes, dot_values.get(),
            dot_result.get(), vertex_count));
        double result = 0.0;
        KCS_CUDA_CHECK(cudaMemcpy(&result, dot_result.get(), sizeof(result),
                                  cudaMemcpyDeviceToHost));
        return result;
    }

    void apply_system(const Solver &solver, const Vec3 *x,
                      float inverse_h_squared, Vec3 *output) {
        apply_system_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
            x, masses.get(), inverse_h_squared, incidence_offsets.get(),
            incidence.get(), constraints.get(), strain_incidence_offsets.get(),
            strain_incidence.get(), strain_constraints.get(),
            active_contacts.get(), solver.contact_weight_, output,
            vertex_count);
    }

    void apply_preconditioner(const Solver &) {
        for (uint32_t color = 0u; color < preconditioner_color_count; ++color) {
            const uint32_t first = preconditioner_offsets_host[color];
            const uint32_t end = preconditioner_offsets_host[color + 1u];
            colored_preconditioner_kernel<<<blocks_for(end - first),
                                            kBlockSize>>>(
                pcg_r.get(), pcg_diagonal.get(), incidence_offsets.get(),
                incidence.get(), constraints.get(), preconditioner_colors.get(),
                preconditioner_vertices.get() + first, color, end - first,
                true, pcg_z.get());
        }
        for (uint32_t reverse = preconditioner_color_count; reverse-- > 0u;) {
            const uint32_t first = preconditioner_offsets_host[reverse];
            const uint32_t end = preconditioner_offsets_host[reverse + 1u];
            colored_preconditioner_kernel<<<blocks_for(end - first),
                                            kBlockSize>>>(
                pcg_r.get(), pcg_diagonal.get(), incidence_offsets.get(),
                incidence.get(), constraints.get(), preconditioner_colors.get(),
                preconditioner_vertices.get() + first, reverse, end - first,
                false, pcg_z.get());
        }
    }

    bool solve_pcg(Solver &solver, float inverse_h_squared) {
        apply_system(solver, iterate.get(), inverse_h_squared, pcg_ap.get());
        diagonal_residual_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
            rhs.get(), pcg_ap.get(), masses.get(), inverse_h_squared,
            incidence_offsets.get(), incidence.get(), constraints.get(),
            strain_incidence_offsets.get(), strain_incidence.get(),
            strain_constraints.get(), active_contacts.get(),
            solver.contact_weight_, pcg_diagonal.get(), pcg_r.get(),
            vertex_count);
        apply_preconditioner(solver);
        KCS_CUDA_CHECK(cudaMemcpy(pcg_p.get(), pcg_z.get(), pcg_p.bytes(),
                                  cudaMemcpyDeviceToDevice));

        const double rhs_norm =
            std::sqrt(std::max(dot(rhs.get(), rhs.get()), 1.0e-30));
        double rz = dot(pcg_r.get(), pcg_z.get());
        double relative =
            std::sqrt(std::max(dot(pcg_r.get(), pcg_r.get()), 0.0)) /
            rhs_norm;
        if (!std::isfinite(relative) || !std::isfinite(rz)) {
            solver.error_ = "CUDA PCG received a non-finite residual";
            return false;
        }
        if (relative <= solver.desc_.pcg_relative_tolerance) {
            solver.stats_.residual = static_cast<float>(relative);
            return true;
        }

        for (uint32_t iteration = 0;
             iteration < solver.desc_.pcg_iterations; ++iteration) {
            apply_system(solver, pcg_p.get(), inverse_h_squared, pcg_ap.get());
            const double denominator = dot(pcg_p.get(), pcg_ap.get());
            if (!(denominator > 1.0e-30) || !std::isfinite(denominator)) {
                solver.error_ = "CUDA PCG system is not positive definite";
                return false;
            }
            const float alpha = static_cast<float>(rz / denominator);
            update_solution_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
                iterate.get(), pcg_r.get(), pcg_p.get(), pcg_ap.get(), alpha,
                vertex_count);
            ++solver.stats_.pcg_iterations;
            relative =
                std::sqrt(std::max(dot(pcg_r.get(), pcg_r.get()), 0.0)) /
                rhs_norm;
            if (!std::isfinite(relative)) {
                solver.error_ = "CUDA PCG produced a non-finite residual";
                return false;
            }
            if (relative <= solver.desc_.pcg_relative_tolerance) break;
            apply_preconditioner(solver);
            const double rz_next = dot(pcg_r.get(), pcg_z.get());
            if (!std::isfinite(rz_next)) {
                solver.error_ =
                    "CUDA PCG preconditioner produced a non-finite value";
                return false;
            }
            const float beta = static_cast<float>(rz_next / rz);
            update_direction_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
                pcg_p.get(), pcg_z.get(), beta, vertex_count);
            rz = rz_next;
        }
        solver.stats_.residual = static_cast<float>(relative);
        if (relative > solver.desc_.pcg_relative_tolerance &&
            solver.cuda_fallback_allowed_) {
            fallback_requested = true;
            return false;
        }
        return true;
    }

    void resolve_collisions(const Solver &solver, const Vec3 *from,
                            Vec3 *candidate, uint8_t *contact_flags,
                            float motion_radius) {
        if (bvh_node_count == 0u) return;
        const float thickness = std::max(solver.material_.thickness, 1.0e-6f);
        const float skin = std::max(1.0e-5f, thickness * 1.0e-3f);
        resolve_collisions_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
            from, candidate, contact_normals.get(), contact_flags, vertex_count,
            thickness, skin, motion_radius, static_substep_vertices.get(),
            static_previous_vertices.get(), static_triangles.get(),
            bvh_order.get(), bvh_nodes.get(),
            static_cast<uint32_t>(bvh_node_count), contacts.get());
    }

    bool step(Solver &solver, float frame_dt) {
        fallback_requested = false;
        constexpr size_t kAutoCudaMinimumVertices = 8192u;
        if (solver.cuda_fallback_allowed_ &&
            vertex_count < kAutoCudaMinimumVertices) {
            fallback_requested = true;
            return false;
        }
        solver.error_.clear();
        solver.stats_ = {};
        solver.stats_.substeps = solver.desc_.substeps;
        solver.stats_.pd_iterations =
            solver.desc_.substeps * solver.desc_.pd_iterations;
        const float h = frame_dt / static_cast<float>(solver.desc_.substeps);
        if (!(h > 0.0f) || !std::isfinite(h)) {
            solver.error_ = "invalid CUDA substep duration";
            return false;
        }
        const float inverse_h_squared = 1.0f / (h * h);
        const float damping = std::exp(-solver.desc_.velocity_damping * h);
        float motion_radius = 0.0f;
        if (solver.static_update_pending_) {
            float maximum_motion_squared = 0.0f;
            for (size_t i = 0; i < solver.static_vertices_.size(); ++i) {
                maximum_motion_squared = std::max(
                    maximum_motion_squared,
                    length_squared(solver.static_target_vertices_[i] -
                                   solver.static_vertices_[i]));
            }
            motion_radius = std::sqrt(maximum_motion_squared) /
                            static_cast<float>(solver.desc_.substeps);
            static_target_vertices.upload(solver.static_target_vertices_);
        }

        contacts.clear();
        limited.clear();
        for (uint32_t substep = 0; substep < solver.desc_.substeps; ++substep) {
            if (strain_enabled) strain_dual.clear();
            if (solver.static_update_pending_) {
                const Vec3 *previous =
                    substep == 0u ? static_vertices.get()
                                  : static_substep_vertices.get();
                KCS_CUDA_CHECK(cudaMemcpy(static_previous_vertices.get(),
                                          previous,
                                          static_previous_vertices.bytes(),
                                          cudaMemcpyDeviceToDevice));
                const float alpha = static_cast<float>(substep + 1u) /
                                    static_cast<float>(solver.desc_.substeps);
                interpolate_static_kernel<<<blocks_for(static_vertex_count),
                                            kBlockSize>>>(
                    static_vertices.get(), static_target_vertices.get(),
                    static_substep_vertices.get(), static_vertex_count, alpha);
                if (static_triangle_count != 0u) {
                    refit_triangles_kernel<<<blocks_for(static_triangle_count),
                                             kBlockSize>>>(
                        static_substep_vertices.get(), static_triangles.get(),
                        static_triangle_count);
                    refit_nodes_kernel<<<1, 1>>>(
                        static_triangles.get(), bvh_order.get(), bvh_nodes.get(),
                        bvh_node_count);
                }
            } else if (substep == 0u && static_vertex_count != 0u) {
                KCS_CUDA_CHECK(cudaMemcpy(static_substep_vertices.get(),
                                          static_vertices.get(),
                                          static_vertices.bytes(),
                                          cudaMemcpyDeviceToDevice));
                KCS_CUDA_CHECK(cudaMemcpy(static_previous_vertices.get(),
                                          static_vertices.get(),
                                          static_vertices.bytes(),
                                          cudaMemcpyDeviceToDevice));
            }

            KCS_CUDA_CHECK(cudaMemcpy(substep_start.get(), positions.get(),
                                      positions.bytes(), cudaMemcpyDeviceToDevice));
            contact_normals.clear();
            contacted.clear();
            predict_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
                velocities.get(), positions.get(), predicted.get(), iterate.get(),
                vertex_count, damping,
                {solver.desc_.gravity.x, solver.desc_.gravity.y,
                 solver.desc_.gravity.z},
                h, h * h);

            for (uint32_t pd = 0; pd < solver.desc_.pd_iterations; ++pd) {
                project_constraints_kernel<<<blocks_for(constraint_count),
                                              kBlockSize>>>(
                    constraints.get(), constraint_count, iterate.get(),
                    rest_positions.get(), projection.get());
                if (strain_enabled) {
                    project_strain_kernel<<<blocks_for(strain_constraint_count),
                                            kBlockSize>>>(
                        strain_constraints.get(), strain_constraint_count,
                        iterate.get(), strain_dual.get(), strain_projection.get(),
                        limited.get());
                }
                KCS_CUDA_CHECK(cudaMemcpy(contact_targets.get(), iterate.get(),
                                          iterate.bytes(), cudaMemcpyDeviceToDevice));
                active_contacts.clear();
                resolve_collisions(solver, substep_start.get(),
                                   contact_targets.get(), active_contacts.get(),
                                   motion_radius);
                build_rhs_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
                    predicted.get(), masses.get(), inverse_h_squared,
                    incidence_offsets.get(), incidence.get(), projection.get(),
                    strain_incidence_offsets.get(), strain_incidence.get(),
                    strain_constraints.get(), strain_projection.get(),
                    strain_dual.get(), active_contacts.get(),
                    contact_targets.get(), solver.contact_weight_, rhs.get(),
                    contacted.get(), vertex_count);
                if (!solve_pcg(solver, inverse_h_squared)) return false;
                if (strain_enabled) {
                    update_strain_duals_kernel<<<
                        blocks_for(strain_constraint_count), kBlockSize>>>(
                        strain_constraints.get(), strain_constraint_count,
                        iterate.get(), strain_projection.get(), strain_dual.get());
                }
            }

            for (uint32_t collision_pass = 0;
                 collision_pass < solver.desc_.collision_iterations;
                 ++collision_pass) {
                resolve_collisions(solver, substep_start.get(), iterate.get(),
                                   contacted.get(), motion_radius);
            }
            finalize_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
                iterate.get(), substep_start.get(), positions.get(),
                velocities.get(), contact_normals.get(), contacted.get(),
                vertex_count, 1.0f / h, solver.material_.restitution,
                solver.material_.friction);
        }

        int valid = 1;
        KCS_CUDA_CHECK(cudaMemcpy(finite_valid.get(), &valid, sizeof(valid),
                                  cudaMemcpyHostToDevice));
        finite_state_kernel<<<blocks_for(vertex_count), kBlockSize>>>(
            positions.get(), velocities.get(), vertex_count, finite_valid.get());
        KCS_CUDA_CHECK(cudaMemcpy(&valid, finite_valid.get(), sizeof(valid),
                                  cudaMemcpyDeviceToHost));
        if (!valid) {
            solver.error_ = "CUDA solver produced a non-finite SHELL state";
            return false;
        }

        float maximum_stretch = 1.0f;
        int maximum_bits = 0;
        std::memcpy(&maximum_bits, &maximum_stretch, sizeof(maximum_bits));
        KCS_CUDA_CHECK(cudaMemcpy(maximum_stretch_bits.get(), &maximum_bits,
                                  sizeof(maximum_bits),
                                  cudaMemcpyHostToDevice));
        if (strain_constraint_count != 0u) {
            maximum_stretch_kernel<<<blocks_for(strain_constraint_count),
                                     kBlockSize>>>(
                strain_constraints.get(), strain_constraint_count,
                positions.get(), maximum_stretch_bits.get());
            KCS_CUDA_CHECK(cudaMemcpy(&maximum_bits,
                                      maximum_stretch_bits.get(),
                                      sizeof(maximum_bits),
                                      cudaMemcpyDeviceToHost));
            std::memcpy(&maximum_stretch, &maximum_bits,
                        sizeof(maximum_stretch));
        }
        solver.stats_.maximum_principal_stretch = maximum_stretch;

        unsigned long long contact_count = 0u;
        unsigned long long limited_count = 0u;
        KCS_CUDA_CHECK(cudaMemcpy(&contact_count, contacts.get(),
                                  sizeof(contact_count),
                                  cudaMemcpyDeviceToHost));
        KCS_CUDA_CHECK(cudaMemcpy(&limited_count, limited.get(),
                                  sizeof(limited_count),
                                  cudaMemcpyDeviceToHost));
        solver.stats_.contacts = contact_count;
        solver.stats_.strain_limit_projections = limited_count;

        positions.download(solver.positions_);
        velocities.download(solver.velocities_);
        if (solver.static_update_pending_) {
            KCS_CUDA_CHECK(cudaMemcpy(static_vertices.get(),
                                      static_target_vertices.get(),
                                      static_vertices.bytes(),
                                      cudaMemcpyDeviceToDevice));
            solver.static_vertices_ = solver.static_target_vertices_;
            solver.static_update_pending_ = false;
        }
        return true;
    }
};

CudaBackend::CudaBackend() : impl_(std::make_unique<Impl>()) {}
CudaBackend::~CudaBackend() = default;

bool CudaBackend::available(std::string *device_name, std::string *error) {
    if (device_name) device_name->clear();
    if (error) error->clear();
    int count = 0;
    const cudaError_t count_result = cudaGetDeviceCount(&count);
    if (count_result != cudaSuccess || count <= 0) {
        if (error) {
            *error = count_result == cudaSuccess
                         ? "no CUDA device was found"
                         : cudaGetErrorString(count_result);
        }
        cudaGetLastError();
        return false;
    }
    int device = 0;
    if (const char *requested = std::getenv("KOROMO_CUDA_DEVICE")) {
        char *end = nullptr;
        const long parsed = std::strtol(requested, &end, 10);
        if (end != requested && *end == '\0' && parsed >= 0 && parsed < count) {
            device = static_cast<int>(parsed);
        }
    }
    cudaDeviceProp properties{};
    const cudaError_t property_result = cudaGetDeviceProperties(&properties, device);
    if (property_result != cudaSuccess) {
        if (error) *error = cudaGetErrorString(property_result);
        cudaGetLastError();
        return false;
    }
    if (device_name) *device_name = properties.name;
    return true;
}

std::unique_ptr<CudaBackend> CudaBackend::create(Solver &solver,
                                                  std::string &error) {
    std::string device_name;
    if (!available(&device_name, &error)) return nullptr;
    try {
        auto backend = std::unique_ptr<CudaBackend>(new CudaBackend());
        int device = 0;
        if (const char *requested = std::getenv("KOROMO_CUDA_DEVICE")) {
            char *end = nullptr;
            const long parsed = std::strtol(requested, &end, 10);
            int count = 0;
            cudaGetDeviceCount(&count);
            if (end != requested && *end == '\0' && parsed >= 0 &&
                parsed < count) {
                device = static_cast<int>(parsed);
            }
        }
        KCS_CUDA_CHECK(cudaSetDevice(device));
        backend->impl_->device = device;
        backend->impl_->initialize(solver);
        return backend;
    } catch (const std::exception &exception) {
        error = std::string("CUDA initialization failed: ") + exception.what();
        return nullptr;
    }
}

bool CudaBackend::step(Solver &solver, float frame_dt) {
    try {
        KCS_CUDA_CHECK(cudaSetDevice(impl_->device));
        return impl_->step(solver, frame_dt);
    } catch (const std::exception &exception) {
        solver.error_ = std::string("CUDA step failed: ") + exception.what();
        return false;
    }
}

bool CudaBackend::fallback_requested() const {
    return impl_->fallback_requested;
}

} // namespace kcs
