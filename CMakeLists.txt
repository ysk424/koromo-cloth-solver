cmake_minimum_required(VERSION 3.20)

project(koromo_cloth_solver VERSION 0.6.0 LANGUAGES C CXX)

option(KOROMO_BUILD_BLENDER_EXTENSION "Enable Blender Extension staging" ON)
set(KOROMO_CUDA "AUTO" CACHE STRING
    "Build the optional CUDA solver DLL: AUTO, ON, or OFF")
set_property(CACHE KOROMO_CUDA PROPERTY STRINGS AUTO ON OFF)
set(KOROMO_CUDA_ARCHITECTURES "75-real;86-real;89-real;120" CACHE STRING
    "CUDA architectures for the optional CUDA solver DLL")
set(KOROMO_BLENDER_EXECUTABLE "" CACHE FILEPATH
    "Blender executable used to package and test the Extension")

find_package(OpenMP REQUIRED COMPONENTS CXX)

string(TOUPPER "${KOROMO_CUDA}" KOROMO_CUDA_MODE)
if(NOT KOROMO_CUDA_MODE MATCHES "^(AUTO|ON|OFF)$")
    message(FATAL_ERROR "KOROMO_CUDA must be AUTO, ON, or OFF")
endif()

set(KOROMO_HAS_CUDA OFF)
if(NOT KOROMO_CUDA_MODE STREQUAL "OFF")
    if(MSVC)
        include(CheckLanguage)
        check_language(CUDA)
        if(CMAKE_CUDA_COMPILER)
            enable_language(CUDA)
            find_package(CUDAToolkit REQUIRED)
            set(KOROMO_HAS_CUDA ON)
            message(STATUS "Koromo CUDA backend enabled: ${CMAKE_CUDA_COMPILER}")
        elseif(KOROMO_CUDA_MODE STREQUAL "ON")
            message(FATAL_ERROR "KOROMO_CUDA=ON but no CUDA compiler was found")
        else()
            message(STATUS "CUDA compiler not found; building CPU/OpenMP only")
        endif()
    elseif(KOROMO_CUDA_MODE STREQUAL "ON")
        message(FATAL_ERROR
            "The Windows CUDA backend requires the MSVC/Visual Studio generator")
    else()
        message(STATUS
            "CUDA AUTO disabled for this non-MSVC build; building CPU/OpenMP only")
    endif()
endif()

function(koromo_configure_solver_target target)
    target_compile_features(${target} PUBLIC cxx_std_17)
    target_compile_definitions(${target} PRIVATE KCS_BUILD_DLL)
    target_include_directories(${target} PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
    )
    set_target_properties(${target} PROPERTIES
        CXX_VISIBILITY_PRESET hidden
        VISIBILITY_INLINES_HIDDEN YES
        WINDOWS_EXPORT_ALL_SYMBOLS NO
    )
    if(WIN32)
        set_target_properties(${target} PROPERTIES PREFIX "")
    endif()

    if(MSVC)
        target_compile_options(${target} PRIVATE
            $<$<COMPILE_LANGUAGE:CXX>:/W4 /permissive- /EHsc>)
        target_link_libraries(${target} PRIVATE OpenMP::OpenMP_CXX)
    elseif(MINGW)
        target_compile_options(${target} PRIVATE
            -Wall -Wextra -Wpedantic ${OpenMP_CXX_FLAGS})
        get_filename_component(GOMP_DIR "${OpenMP_gomp_LIBRARY}" DIRECTORY)
        get_filename_component(THREAD_DIR "${OpenMP_mingwthrd_LIBRARY}" DIRECTORY)
        target_link_libraries(${target} PRIVATE
            "${GOMP_DIR}/libgomp.a" "${THREAD_DIR}/libdl.a"
            "${OpenMP_mingwthrd_LIBRARY}" "${THREAD_DIR}/libwinpthread.a")
        target_link_options(${target} PRIVATE -static-libgcc -static-libstdc++)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
        target_link_libraries(${target} PRIVATE OpenMP::OpenMP_CXX)
    endif()
endfunction()

# The native core is part of this repository. No sibling solver checkout or
# solver-source path is needed to configure or build Koromo.
add_library(koromo_cloth_solver SHARED
    src/api.cpp
    src/solver.cpp
)
koromo_configure_solver_target(koromo_cloth_solver)

if(KOROMO_HAS_CUDA)
    add_library(koromo_cloth_solver_cuda SHARED
        src/api.cpp
        src/solver.cpp
        src/cuda_backend.cu
    )
    koromo_configure_solver_target(koromo_cloth_solver_cuda)
    target_compile_definitions(koromo_cloth_solver_cuda PRIVATE KCS_HAS_CUDA=1)
    target_compile_features(koromo_cloth_solver_cuda PRIVATE cuda_std_17)
    target_compile_options(koromo_cloth_solver_cuda PRIVATE
        $<$<COMPILE_LANGUAGE:CUDA>:--expt-relaxed-constexpr>)
    target_link_libraries(koromo_cloth_solver_cuda PRIVATE CUDA::cudart_static)
    set_target_properties(koromo_cloth_solver_cuda PROPERTIES
        OUTPUT_NAME koromo_cloth_solver_cuda
        CUDA_ARCHITECTURES "${KOROMO_CUDA_ARCHITECTURES}"
        CUDA_RUNTIME_LIBRARY Static
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
endif()

enable_testing()
add_executable(koromo_cloth_solver_tests tests/test_c_api.c)
target_compile_features(koromo_cloth_solver_tests PRIVATE c_std_11)
target_include_directories(koromo_cloth_solver_tests PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(koromo_cloth_solver_tests PRIVATE koromo_cloth_solver)
add_test(NAME koromo_cloth_solver_c_api COMMAND koromo_cloth_solver_tests)

add_executable(koromo_cloth_solver_core_tests tests/test_solver.cpp)
target_compile_features(koromo_cloth_solver_core_tests PRIVATE cxx_std_17)
target_include_directories(koromo_cloth_solver_core_tests PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(koromo_cloth_solver_core_tests PRIVATE
    koromo_cloth_solver)
add_test(NAME koromo_cloth_solver_core
    COMMAND koromo_cloth_solver_core_tests)

if(TARGET koromo_cloth_solver_cuda)
    add_executable(koromo_cloth_solver_cuda_tests tests/test_c_api.c)
    target_compile_features(koromo_cloth_solver_cuda_tests PRIVATE c_std_11)
    target_include_directories(koromo_cloth_solver_cuda_tests PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include")
    target_link_libraries(koromo_cloth_solver_cuda_tests PRIVATE
        koromo_cloth_solver_cuda)
    add_test(NAME koromo_cloth_solver_cuda_c_api
        COMMAND koromo_cloth_solver_cuda_tests)

    add_executable(koromo_cloth_solver_cuda_core_tests tests/test_solver.cpp)
    target_compile_features(koromo_cloth_solver_cuda_core_tests PRIVATE cxx_std_17)
    target_include_directories(koromo_cloth_solver_cuda_core_tests PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include")
    target_link_libraries(koromo_cloth_solver_cuda_core_tests PRIVATE
        koromo_cloth_solver_cuda)
    add_test(NAME koromo_cloth_solver_cuda_core
        COMMAND koromo_cloth_solver_cuda_core_tests)

    find_package(Python3 QUIET COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(NAME koromo_cloth_solver_cuda_auto
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/cuda_auto_smoke.py"
                --cpu $<TARGET_FILE:koromo_cloth_solver>
                --cuda $<TARGET_FILE:koromo_cloth_solver_cuda>
                --native-dir
                    "${CMAKE_CURRENT_SOURCE_DIR}/blender_extension/koromo_cloth_solver")
    endif()
endif()

set(BLENDER_BRIDGE_OUT "${CMAKE_CURRENT_BINARY_DIR}/blender_bridge/native.py")
add_custom_command(OUTPUT "${BLENDER_BRIDGE_OUT}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/blender_bridge"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${CMAKE_CURRENT_SOURCE_DIR}/blender_bridge/native.py"
            "${BLENDER_BRIDGE_OUT}"
    DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/blender_bridge/native.py"
    COMMENT "Copying Blender ctypes bridge next to the DLL")
add_custom_target(blender_bridge ALL DEPENDS "${BLENDER_BRIDGE_OUT}")
add_dependencies(blender_bridge koromo_cloth_solver)

if(KOROMO_BUILD_BLENDER_EXTENSION AND WIN32)
    set(KOROMO_EXTENSION_SOURCE
        "${CMAKE_CURRENT_SOURCE_DIR}/blender_extension/koromo_cloth_solver")
    if(TARGET koromo_cloth_solver_cuda)
        set(KOROMO_EXTENSION_ID "koromo_cloth_solver_cuda")
        set(KOROMO_EXTENSION_NAME "Koromo CUDA Experimental")
    else()
        set(KOROMO_EXTENSION_ID "koromo_cloth_solver")
        set(KOROMO_EXTENSION_NAME "Koromo")
    endif()
    set(KOROMO_EXTENSION_STAGE
        "${CMAKE_CURRENT_BINARY_DIR}/blender-extension/${KOROMO_EXTENSION_ID}")
    set(KOROMO_EXTENSION_PACKAGE_DIR "${CMAKE_CURRENT_BINARY_DIR}/packages")
    set(KOROMO_EXTENSION_PACKAGE
        "${KOROMO_EXTENSION_PACKAGE_DIR}/${KOROMO_EXTENSION_ID}-${PROJECT_VERSION}-windows-x64.zip")
    file(GLOB KOROMO_EXTENSION_FILES CONFIGURE_DEPENDS
        "${KOROMO_EXTENSION_SOURCE}/*.py"
        "${KOROMO_EXTENSION_SOURCE}/*.toml"
        "${KOROMO_EXTENSION_SOURCE}/README.md"
        "${KOROMO_EXTENSION_SOURCE}/LICENSE"
        "${KOROMO_EXTENSION_SOURCE}/THIRD_PARTY_NOTICES.md")

    set(KOROMO_EXTENSION_NATIVE_DEPENDS koromo_cloth_solver)
    set(KOROMO_CUDA_STAGE_ARGUMENT "-DKOROMO_CUDA_SOLVER_LIBRARY=")
    if(TARGET koromo_cloth_solver_cuda)
        list(APPEND KOROMO_EXTENSION_NATIVE_DEPENDS koromo_cloth_solver_cuda)
        set(KOROMO_CUDA_STAGE_ARGUMENT
            "-DKOROMO_CUDA_SOLVER_LIBRARY=$<TARGET_FILE:koromo_cloth_solver_cuda>")
    endif()

    add_custom_command(
        OUTPUT "${KOROMO_EXTENSION_STAGE}/.stage-complete"
        COMMAND "${CMAKE_COMMAND}"
            "-DKOROMO_EXTENSION_SOURCE=${KOROMO_EXTENSION_SOURCE}"
            "-DKOROMO_EXTENSION_STAGE=${KOROMO_EXTENSION_STAGE}"
            "-DKOROMO_EXTENSION_ID=${KOROMO_EXTENSION_ID}"
            "-DKOROMO_EXTENSION_NAME=${KOROMO_EXTENSION_NAME}"
            "-DKOROMO_SOLVER_LIBRARY=$<TARGET_FILE:koromo_cloth_solver>"
            "${KOROMO_CUDA_STAGE_ARGUMENT}"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/stage_blender_extension.cmake"
        DEPENDS ${KOROMO_EXTENSION_NATIVE_DEPENDS} ${KOROMO_EXTENSION_FILES}
        BYPRODUCTS
            "${KOROMO_EXTENSION_STAGE}/blender_manifest.toml"
            "${KOROMO_EXTENSION_STAGE}/bin/koromo_cloth_solver.dll"
        COMMENT "Staging the Koromo Blender Extension"
        VERBATIM)
    add_custom_target(stage-blender-extension
        DEPENDS "${KOROMO_EXTENSION_STAGE}/.stage-complete")

    if(KOROMO_BLENDER_EXECUTABLE)
        if(NOT EXISTS "${KOROMO_BLENDER_EXECUTABLE}")
            message(FATAL_ERROR
                "KOROMO_BLENDER_EXECUTABLE does not exist: ${KOROMO_BLENDER_EXECUTABLE}")
        endif()
        add_custom_target(blender-extension
            COMMAND "${CMAKE_COMMAND}" -E make_directory
                    "${KOROMO_EXTENSION_PACKAGE_DIR}"
            COMMAND "${KOROMO_BLENDER_EXECUTABLE}" --command extension build
                    --source-dir "${KOROMO_EXTENSION_STAGE}"
                    --output-filepath "${KOROMO_EXTENSION_PACKAGE}"
            DEPENDS stage-blender-extension
            BYPRODUCTS "${KOROMO_EXTENSION_PACKAGE}"
            COMMENT "Building the installable Blender Extension package"
            VERBATIM)
        add_custom_target(blender-extension-test
            COMMAND "${KOROMO_BLENDER_EXECUTABLE}"
                    --background --factory-startup
                    --python-exit-code 1
                    --python
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/blender_extension_smoke.py"
                    -- --extension-stage "${KOROMO_EXTENSION_STAGE}"
            DEPENDS stage-blender-extension
            COMMENT "Running the Blender Extension smoke test"
            VERBATIM)
    else()
        message(STATUS
            "Set KOROMO_BLENDER_EXECUTABLE to enable Extension packaging and tests")
    endif()
elseif(KOROMO_BUILD_BLENDER_EXTENSION)
    message(STATUS "The bundled Blender Extension currently targets Windows x64")
endif()
