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
option(RDT_ENABLE_CUDA "Build the worker with the whisper.cpp CUDA backend" OFF)
option(RDT_ENABLE_VULKAN "Build the worker with the whisper.cpp Vulkan backend" OFF)
if(RDT_ENABLE_CUDA AND RDT_ENABLE_VULKAN)
  message(FATAL_ERROR "Build CUDA and Vulkan workers separately")
endif()
if(RDT_ENABLE_CUDA)
  if(RDT_ENABLE_VULKAN)
    message(FATAL_ERROR "Build CUDA and Vulkan workers separately")
  endif()
  set(GGML_CUDA ON CACHE BOOL "" FORCE)
  message(STATUS "RDT CUDA backend requested")
else()
  set(GGML_CUDA OFF CACHE BOOL "" FORCE)
endif()
if(RDT_ENABLE_VULKAN)
  # Never silently produce a CPU-only worker when Vulkan was requested.
  find_program(RDT_GLSLC_EXECUTABLE
    NAMES glslc
    HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
  if(NOT RDT_GLSLC_EXECUTABLE)
    message(FATAL_ERROR
      "RDT_ENABLE_VULKAN=ON requires the Vulkan SDK shader compiler (glslc). "
      "Install the Vulkan SDK and ensure glslc is discoverable.")
  endif()
  set(GGML_VULKAN ON CACHE BOOL "" FORCE)
  message(STATUS "RDT Vulkan backend requested; glslc: ${RDT_GLSLC_EXECUTABLE}")
else()
  set(GGML_VULKAN OFF CACHE BOOL "" FORCE)
  message(STATUS "RDT Vulkan backend disabled; building CPU qualification worker")
endif()
set(GGML_OPENCL OFF CACHE BOOL "" FORCE)
set(GGML_SYCL OFF CACHE BOOL "" FORCE)
set(GGML_HIP OFF CACHE BOOL "" FORCE)
set(GGML_METAL OFF CACHE BOOL "" FORCE)
option(RDT_ENABLE_ARM_DOTPROD
  "Build an ARM64 worker optimized for ARM dot-product instructions" OFF)

if(MSVC AND CMAKE_GENERATOR_PLATFORM STREQUAL "ARM64")
  # MSVC ARM requires GGML_NATIVE=ON. The portable worker keeps optional
  # instructions disabled. The separate optimized worker explicitly enables
  # dot-product code; Python selects it only after a Windows runtime CPU check.
  set(GGML_NATIVE ON CACHE BOOL "" FORCE)
  if(RDT_ENABLE_ARM_DOTPROD)
    # Test compiler/intrinsic support without executing dot-product code on
    # the build host. Runtime selection is separately gated by Windows.
    include(CheckCXXSourceCompiles)
    set(_RDT_SAVED_REQUIRED_FLAGS "${CMAKE_REQUIRED_FLAGS}")
    string(APPEND CMAKE_REQUIRED_FLAGS " /arch:armv8.2")
    unset(RDT_COMPILER_SUPPORTS_ARM_DOTPROD CACHE)
    check_cxx_source_compiles(
      "#include <arm_neon.h>
       int main() {
         int8x16_t a = vdupq_n_s8(0), b = vdupq_n_s8(0);
         int32x4_t sum = vdupq_n_s32(0);
         sum = vdotq_s32(sum, a, b);
         return vgetq_lane_s32(sum, 0);
       }"
      RDT_COMPILER_SUPPORTS_ARM_DOTPROD
    )
    set(CMAKE_REQUIRED_FLAGS "${_RDT_SAVED_REQUIRED_FLAGS}")
    unset(_RDT_SAVED_REQUIRED_FLAGS)
    if(NOT RDT_COMPILER_SUPPORTS_ARM_DOTPROD)
      message(FATAL_ERROR
        "The compiler cannot build the optional ARM dot-product worker")
    endif()
    set(GGML_MACHINE_SUPPORTS_dotprod ON CACHE BOOL "" FORCE)
    foreach(feature i8mm sve sme)
      set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)
    endforeach()
    message(STATUS "RDT ARM dot-product compiler check passed")
  else()
    foreach(feature dotprod i8mm sve sme)
      set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)
    endforeach()
    message(STATUS "RDT portable ARM64 baseline worker")
  endif()
else()
  if(RDT_ENABLE_ARM_DOTPROD)
    message(FATAL_ERROR "RDT_ENABLE_ARM_DOTPROD requires MSVC ARM64")
  endif()
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
endif()
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory("${WHISPER_CPP_SOURCE}" whisper.cpp)

if(MSVC AND CMAKE_GENERATOR_PLATFORM STREQUAL "ARM64"
   AND RDT_ENABLE_ARM_DOTPROD)
  # Apply the ARMv8.2 target to the actual GGML CPU implementation,
  # not just to the compiler capability test.
  if(NOT TARGET ggml-cpu)
    message(FATAL_ERROR
      "Optimized ARM build expected the ggml-cpu target, but it was not created")
  endif()
  target_compile_options(ggml-cpu PRIVATE /arch:armv8.2)
  message(STATUS "RDT ARM dot-product target enabled on ggml-cpu")
endif()

add_executable(rdt_whisper_worker rdt_whisper_worker.cpp)
target_compile_features(rdt_whisper_worker PRIVATE cxx_std_17)
target_include_directories(rdt_whisper_worker PRIVATE "${WHISPER_CPP_SOURCE}/ggml/include")
target_link_libraries(rdt_whisper_worker PRIVATE whisper)
target_compile_definitions(rdt_whisper_worker PRIVATE
  RDT_ENABLE_CUDA=$<BOOL:${RDT_ENABLE_CUDA}>
  RDT_ENABLE_VULKAN=$<BOOL:${RDT_ENABLE_VULKAN}>
)
if(MSVC)
  target_compile_options(rdt_whisper_worker PRIVATE /W4 /permissive- /EHsc)
  target_compile_definitions(rdt_whisper_worker PRIVATE _CRT_SECURE_NO_WARNINGS NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
set_target_properties(rdt_whisper_worker PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_MINSIZEREL "${CMAKE_BINARY_DIR}/bin")
