cmake_minimum_required(VERSION 3.24)
project(rdt_whisper_worker LANGUAGES C CXX)

# Same worker source is compiled natively per OS/architecture. Runtime and
# GPU options are build-matrix inputs, never inferred from a detected DLL/device.

set(RDT_PINNED_WHISPER_CPP_COMMIT "d1be6fde11ac6e0407606b4e42fe72d34add8037")

if(DEFINED RDT_WHISPER_CPP_COMMIT)
  if(NOT "${RDT_WHISPER_CPP_COMMIT}" STREQUAL "${RDT_PINNED_WHISPER_CPP_COMMIT}")
    message(FATAL_ERROR
      "RDT_WHISPER_CPP_COMMIT must equal the pinned v1.9.5 revision ${RDT_PINNED_WHISPER_CPP_COMMIT}")
  endif()
endif()
option(RDT_ALLOW_UNVERIFIED_SOURCE_FOR_TESTS
  "Allow a non-Git CMake stub for parser/configuration tests only (never use to build a release worker)" OFF)
if(NOT DEFINED WHISPER_CPP_SOURCE OR NOT EXISTS "${WHISPER_CPP_SOURCE}/CMakeLists.txt")
  message(FATAL_ERROR "WHISPER_CPP_SOURCE must point at the pinned whisper.cpp v1.9.5 source tree")
endif()
# Enforce the actual checkout revision even when callers omit the commit argument.
execute_process(
  COMMAND git -C "${WHISPER_CPP_SOURCE}" rev-parse HEAD
  RESULT_VARIABLE RDT_WHISPER_CPP_GIT_RESULT
  OUTPUT_VARIABLE RDT_WHISPER_CPP_GIT_COMMIT
  OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET
)
if(NOT RDT_WHISPER_CPP_GIT_RESULT EQUAL 0)
  message(FATAL_ERROR "Could not verify the whisper.cpp Git revision.")
endif()
if(NOT "${RDT_WHISPER_CPP_GIT_COMMIT}" STREQUAL "${RDT_PINNED_WHISPER_CPP_COMMIT}")
  message(FATAL_ERROR "WHISPER_CPP_SOURCE is not the pinned whisper.cpp v1.9.5 revision.")
endif()
if(NOT DEFINED RDT_WHISPER_CPP_COMMIT OR "${RDT_WHISPER_CPP_COMMIT}" STREQUAL "")
  set(RDT_WHISPER_CPP_COMMIT "${RDT_WHISPER_CPP_GIT_COMMIT}")
endif()


if(EXISTS "${WHISPER_CPP_SOURCE}/.git" OR IS_SYMLINK "${WHISPER_CPP_SOURCE}/.git")
  find_package(Git REQUIRED)
  execute_process(
    COMMAND "${GIT_EXECUTABLE}" -C "${WHISPER_CPP_SOURCE}" rev-parse --verify HEAD
    RESULT_VARIABLE RDT_GIT_HEAD_STATUS OUTPUT_VARIABLE RDT_GIT_HEAD OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(NOT RDT_GIT_HEAD_STATUS EQUAL 0 OR NOT RDT_GIT_HEAD STREQUAL RDT_PINNED_WHISPER_CPP_COMMIT)
    message(FATAL_ERROR "WHISPER_CPP_SOURCE is not at the pinned commit ${RDT_PINNED_WHISPER_CPP_COMMIT}")
  endif()
  execute_process(
    COMMAND "${GIT_EXECUTABLE}" -C "${WHISPER_CPP_SOURCE}" status --porcelain --untracked-files=all
    RESULT_VARIABLE RDT_GIT_STATUS OUTPUT_VARIABLE RDT_GIT_DIRTY OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(NOT RDT_GIT_STATUS EQUAL 0 OR NOT RDT_GIT_DIRTY STREQUAL "")
    message(FATAL_ERROR "WHISPER_CPP_SOURCE must be clean; dirty source invalidates the pinned revision")
  endif()
elseif(NOT RDT_ALLOW_UNVERIFIED_SOURCE_FOR_TESTS)
  message(FATAL_ERROR "WHISPER_CPP_SOURCE must be a clean Git checkout at the pinned revision")
else()
  message(WARNING "UNVERIFIED TEST STUB ENABLED: this configuration is not eligible for a release worker")
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
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux"
       AND CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|ARM64)$"
       AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  # Build a portable ARM64 baseline by default. A separate dot-product worker
  # is allowed only after a compile-time intrinsic check; engine.py selects it
  # at runtime only when /proc/cpuinfo advertises ASIMDDP/DOTPROD.
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
  if(RDT_ENABLE_ARM_DOTPROD)
    include(CheckCXXSourceCompiles)
    set(_RDT_SAVED_REQUIRED_FLAGS "${CMAKE_REQUIRED_FLAGS}")
    string(APPEND CMAKE_REQUIRED_FLAGS " -march=armv8.2-a+dotprod")
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
        "The Linux ARM64 compiler cannot build the optional ARM dot-product worker")
    endif()
    set(GGML_MACHINE_SUPPORTS_dotprod ON CACHE BOOL "" FORCE)
    foreach(feature i8mm sve sme)
      set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)
    endforeach()
    message(STATUS "RDT Linux ARM dot-product compiler check passed")
  else()
    foreach(feature dotprod i8mm sve sme)
      set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)
    endforeach()
    message(STATUS "RDT Linux portable ARM64 baseline worker")
  endif()
else()
  if(RDT_ENABLE_ARM_DOTPROD)
    message(FATAL_ERROR
      "RDT_ENABLE_ARM_DOTPROD requires MSVC ARM64 or Linux ARM64 with GCC/Clang")
  endif()
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
endif()

# The Linux amd64 package ships one worker under the generic CPU name. Keep
# that worker at the x86-64-v1 baseline: GGML_NATIVE=OFF does not disable all
# optional x86 ISA defaults in the pinned upstream build. Optimized workers
# must have distinct names, runtime feature gates, and independent inference
# qualification before they can be shipped.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux"
   AND CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64)$")
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
  foreach(feature SSE42 AVX AVX_VNNI AVX2 BMI2 FMA F16C AVX512 AVX512_VBMI AVX512_VNNI AVX512_BF16 AMX_TILE AMX_INT8 AMX_BF16)
    set("GGML_${feature}" OFF CACHE BOOL "" FORCE)
  endforeach()
  message(STATUS "RDT Linux portable x86-64-v1 worker; optional x86 ISA features disabled")
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
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux"
       AND CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|ARM64)$"
       AND RDT_ENABLE_ARM_DOTPROD)
  # Scope ARMv8.2 instructions to GGML's CPU implementation so the entire
  # worker does not need to assume the optimized CPU feature set.
  if(NOT TARGET ggml-cpu)
    message(FATAL_ERROR
      "RDT Linux ARM dot-product build expected the ggml-cpu target, but it was not created")
  endif()
  target_compile_options(ggml-cpu PRIVATE -march=armv8.2-a+dotprod)
  message(STATUS "RDT Linux ARM dot-product target enabled on ggml-cpu")
endif()

add_executable(rdt_whisper_worker rdt_whisper_worker.cpp)
target_compile_features(rdt_whisper_worker PRIVATE cxx_std_17)
target_include_directories(rdt_whisper_worker PRIVATE "${WHISPER_CPP_SOURCE}/ggml/include")
target_link_libraries(rdt_whisper_worker PRIVATE whisper)
target_compile_definitions(rdt_whisper_worker PRIVATE
  RDT_WHISPER_CPP_COMMIT="${RDT_WHISPER_CPP_COMMIT}"
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
