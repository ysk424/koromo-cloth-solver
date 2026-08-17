#ifndef KCS_CUDA_BACKEND_HPP
#define KCS_CUDA_BACKEND_HPP

#include <memory>
#include <string>

namespace kcs {

class Solver;

class CudaBackend {
public:
    static bool available(std::string *device_name = nullptr,
                          std::string *error = nullptr);
    static std::unique_ptr<CudaBackend> create(Solver &solver,
                                               std::string &error);

    ~CudaBackend();
    CudaBackend(const CudaBackend &) = delete;
    CudaBackend &operator=(const CudaBackend &) = delete;

    bool step(Solver &solver, float frame_dt);
    bool fallback_requested() const;

private:
    CudaBackend();
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace kcs

#endif
