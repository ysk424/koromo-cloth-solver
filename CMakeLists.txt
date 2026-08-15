cmake_minimum_required(VERSION 3.20)

project(yarn_level_knitware_solver VERSION 0.3.1 LANGUAGES C CXX)

option(YLKS_BUILD_BLENDER_EXTENSION "Enable Blender Extension staging" ON)
set(YLKS_BLENDER_EXECUTABLE "" CACHE FILEPATH
    "Blender executable used to package and test the Extension")

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

add_executable(yarn_level_knitware_solver_core_tests
    "${OCS_ROOT}/tests/test_solver.cpp")
target_compile_features(yarn_level_knitware_solver_core_tests PRIVATE cxx_std_17)
target_include_directories(yarn_level_knitware_solver_core_tests PRIVATE
    "${OCS_ROOT}/include")
target_link_libraries(yarn_level_knitware_solver_core_tests PRIVATE
    yarn_level_knitware_solver)
add_test(NAME yarn_level_knitware_solver_core
    COMMAND yarn_level_knitware_solver_core_tests)

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

if(YLKS_BUILD_BLENDER_EXTENSION AND WIN32)
    set(YLKS_EXTENSION_SOURCE
        "${CMAKE_CURRENT_SOURCE_DIR}/blender_extension/yarn_level_knitware_solver")
    set(YLKS_EXTENSION_STAGE
        "${CMAKE_CURRENT_BINARY_DIR}/blender-extension/yarn_level_knitware_solver")
    set(YLKS_EXTENSION_PACKAGE_DIR "${CMAKE_CURRENT_BINARY_DIR}/packages")
    set(YLKS_EXTENSION_PACKAGE
        "${YLKS_EXTENSION_PACKAGE_DIR}/yarn_level_knitware_solver-${PROJECT_VERSION}-windows-x64.zip")
    file(GLOB YLKS_EXTENSION_FILES CONFIGURE_DEPENDS
        "${YLKS_EXTENSION_SOURCE}/*.py"
        "${YLKS_EXTENSION_SOURCE}/*.toml"
        "${YLKS_EXTENSION_SOURCE}/README.md"
        "${YLKS_EXTENSION_SOURCE}/LICENSE"
        "${YLKS_EXTENSION_SOURCE}/THIRD_PARTY_NOTICES.md")

    add_custom_command(
        OUTPUT "${YLKS_EXTENSION_STAGE}/.stage-complete"
        COMMAND "${CMAKE_COMMAND}"
            "-DYLKS_EXTENSION_SOURCE=${YLKS_EXTENSION_SOURCE}"
            "-DYLKS_EXTENSION_STAGE=${YLKS_EXTENSION_STAGE}"
            "-DYLKS_SOLVER_LIBRARY=$<TARGET_FILE:yarn_level_knitware_solver>"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/stage_blender_extension.cmake"
        DEPENDS yarn_level_knitware_solver ${YLKS_EXTENSION_FILES}
        BYPRODUCTS
            "${YLKS_EXTENSION_STAGE}/blender_manifest.toml"
            "${YLKS_EXTENSION_STAGE}/bin/yarn_level_knitware_solver.dll"
        COMMENT "Staging the Yarn-level Knitwear Solver Blender Extension"
        VERBATIM)
    add_custom_target(stage-blender-extension
        DEPENDS "${YLKS_EXTENSION_STAGE}/.stage-complete")

    if(YLKS_BLENDER_EXECUTABLE)
        if(NOT EXISTS "${YLKS_BLENDER_EXECUTABLE}")
            message(FATAL_ERROR
                "YLKS_BLENDER_EXECUTABLE does not exist: ${YLKS_BLENDER_EXECUTABLE}")
        endif()
        add_custom_target(blender-extension
            COMMAND "${CMAKE_COMMAND}" -E make_directory
                    "${YLKS_EXTENSION_PACKAGE_DIR}"
            COMMAND "${YLKS_BLENDER_EXECUTABLE}" --command extension build
                    --source-dir "${YLKS_EXTENSION_STAGE}"
                    --output-filepath "${YLKS_EXTENSION_PACKAGE}"
            DEPENDS stage-blender-extension
            BYPRODUCTS "${YLKS_EXTENSION_PACKAGE}"
            COMMENT "Building the installable Blender Extension package"
            VERBATIM)
        add_custom_target(blender-extension-test
            COMMAND "${YLKS_BLENDER_EXECUTABLE}"
                    --background --factory-startup
                    --python-exit-code 1
                    --python
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/blender_extension_smoke.py"
                    -- --extension-stage "${YLKS_EXTENSION_STAGE}"
            DEPENDS stage-blender-extension
            COMMENT "Running the Blender Extension smoke test"
            VERBATIM)
    else()
        message(STATUS
            "Set YLKS_BLENDER_EXECUTABLE to enable Extension packaging and tests")
    endif()
elseif(YLKS_BUILD_BLENDER_EXTENSION)
    message(STATUS "The bundled Blender Extension currently targets Windows x64")
endif()
