cmake_minimum_required(VERSION 3.10)
project(falcon_seeded C)

# The seeded Falcon-512 key generation library (see README.md). Built the same
# way on Linux and Windows so CI and the installers share one recipe:
#   cmake -S native/falcon_seeded -B build -DCMAKE_INSTALL_PREFIX=<prefix>
#   cmake --build build --config Release && cmake --install build --config Release
# which gives <prefix>/lib/libfalcon_seeded.so (Windows: <prefix>/bin/falcon_seeded.dll).

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_C_VISIBILITY_PRESET hidden)

add_library(falcon_seeded SHARED
  seeded.c keygen.c codec.c common.c fft.c fpr.c rng.c vrfy.c keccak.c)
set_target_properties(falcon_seeded PROPERTIES C_STANDARD 99)

include(GNUInstallDirs)
install(TARGETS falcon_seeded
  RUNTIME DESTINATION bin
  LIBRARY DESTINATION lib
  ARCHIVE DESTINATION lib)
