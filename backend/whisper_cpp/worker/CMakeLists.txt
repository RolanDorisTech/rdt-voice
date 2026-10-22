cmake_minimum_required(VERSION 3.24)
project(rdt_whisper_worker LANGUAGES C CXX)

# Same worker source is compiled natively per OS/architecture. Runtime and
# GPU options are build-matrix inputs, never inferred from a detected DLL/device.

if(NOT DEFINED WHISPER_CPP_SOURCE OR NOT EXISTS "${WHISPER_CPP_SOURCE}/CMakeLists.txt")
  message(FATAL_ERROR "WHISPER_CPP_SOURCE must point at the pinned whisper.cpp v1.9.5 source tree")
endif()

set(WHISPER_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(WHISPER_INSTALL OFF CACHE BOOL "" FORCE)
set(GGML_CUDA OFF CACHE BOOL "" FORCE)
set(GGML_VULKAN OFF CACHE BOOL "" FORCE)
set(GGML_OPENCL OFF CACHE BOOL "" FORCE)
set(GGML_SYCL OFF CACHE BOOL "" FORCE)
set(GGML_HIP OFF CACHE BOOL "" FORCE)
set(GGML_METAL OFF CACHE BOOL "" FORCE)
if(MSVC AND CMAKE_GENERATOR_PLATFORM STREQUAL "ARM64")
  # MSVC ARM requires GGML_NATIVE=ON, but pinned GGML detects optional
  # features on the build host. Do not bake host-specific optional ARM
  # instructions into binaries intended for other ARM64 devices and VMs.
  # Keep mandatory ARMv8-A features; disable optional feature probes.
  set(GGML_NATIVE ON CACHE BOOL "" FORCE)
  foreach(feature dotprod i8mm sve sme)
    set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)
  endforeach()
else()
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
endif()
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory("${WHISPER_CPP_SOURCE}" whisper.cpp)

add_executable(rdt_whisper_worker rdt_whisper_worker.cpp)
target_compile_features(rdt_whisper_worker PRIVATE cxx_std_17)
target_link_libraries(rdt_whisper_worker PRIVATE whisper)
if(MSVC)
  target_compile_options(rdt_whisper_worker PRIVATE /W4 /permissive- /EHsc)
  target_compile_definitions(rdt_whisper_worker PRIVATE _CRT_SECURE_NO_WARNINGS NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
set_target_properties(rdt_whisper_worker PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_MINSIZEREL "${CMAKE_BINARY_DIR}/bin")
