cmake_minimum_required(VERSION 3.20)

project(yarn_level_knitware_solver VERSION 0.1.0 LANGUAGES C CXX)

set(OCS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../omp-contact-solver" CACHE PATH
    "Path to the validated cloth/contact solver core")
if(NOT EXISTS "${OCS_ROOT}/src/api.cpp")
    message(FATAL_ERROR "omp-contact-solver core not found at ${OCS_ROOT}")
endif()

find_package(OpenMP REQUIRED COMPONENTS CXX)

# The new DLL owns its copy of the native core.  It has no runtime dependency
# on omp_contact_solver.dll, while retaining the proven ABI used by Blender.
add_library(yarn_level_knitware_solver SHARED
    "${OCS_ROOT}/src/api.cpp"
    "${OCS_ROOT}/src/solver.cpp"
)
target_compile_features(yarn_level_knitware_solver PUBLIC cxx_std_17)
target_compile_definitions(yarn_level_knitware_solver PRIVATE OCS_BUILD_DLL)
target_include_directories(yarn_level_knitware_solver PRIVATE
    "${OCS_ROOT}/include"
    "${OCS_ROOT}/src"
)
set_target_properties(yarn_level_knitware_solver PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
    WINDOWS_EXPORT_ALL_SYMBOLS NO
)
if(WIN32)
    set_target_properties(yarn_level_knitware_solver PROPERTIES PREFIX "")
endif()

if(MSVC)
    target_compile_options(yarn_level_knitware_solver PRIVATE /W4 /permissive- /EHsc)
    target_link_libraries(yarn_level_knitware_solver PRIVATE OpenMP::OpenMP_CXX)
elseif(MINGW)
    target_compile_options(yarn_level_knitware_solver PRIVATE -Wall -Wextra -Wpedantic ${OpenMP_CXX_FLAGS})
    get_filename_component(GOMP_DIR "${OpenMP_gomp_LIBRARY}" DIRECTORY)
    get_filename_component(THREAD_DIR "${OpenMP_mingwthrd_LIBRARY}" DIRECTORY)
    target_link_libraries(yarn_level_knitware_solver PRIVATE
        "${GOMP_DIR}/libgomp.a" "${THREAD_DIR}/libdl.a"
        "${OpenMP_mingwthrd_LIBRARY}" "${THREAD_DIR}/libwinpthread.a")
    target_link_options(yarn_level_knitware_solver PRIVATE -static-libgcc -static-libstdc++)
else()
    target_compile_options(yarn_level_knitware_solver PRIVATE -Wall -Wextra -Wpedantic)
    target_link_libraries(yarn_level_knitware_solver PRIVATE OpenMP::OpenMP_CXX)
endif()

enable_testing()
add_executable(yarn_level_knitware_solver_tests "${OCS_ROOT}/tests/test_c_api.c")
target_compile_features(yarn_level_knitware_solver_tests PRIVATE c_std_11)
target_include_directories(yarn_level_knitware_solver_tests PRIVATE "${OCS_ROOT}/include")
target_link_libraries(yarn_level_knitware_solver_tests PRIVATE yarn_level_knitware_solver)
add_test(NAME yarn_level_knitware_solver_c_api COMMAND yarn_level_knitware_solver_tests)

set(BLENDER_BRIDGE_OUT "${CMAKE_CURRENT_BINARY_DIR}/blender_bridge/native.py")
add_custom_command(OUTPUT "${BLENDER_BRIDGE_OUT}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/blender_bridge"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${CMAKE_CURRENT_SOURCE_DIR}/blender_bridge/native.py"
            "${BLENDER_BRIDGE_OUT}"
    DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/blender_bridge/native.py"
    COMMENT "Copying Blender ctypes bridge next to the DLL")
add_custom_target(blender_bridge ALL DEPENDS "${BLENDER_BRIDGE_OUT}")
add_dependencies(blender_bridge yarn_level_knitware_solver)
