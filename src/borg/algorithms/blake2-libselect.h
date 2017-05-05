/**
 * Selects between system libb2 or vendored blake2 implementation.
 * BORG_USE_LIBB2 is defined by the build driver (setup.py) if
 * libb2 was detected and added to the include path.
 */
#ifdef BORG_USE_LIBB2
#include <blake2.h>
#else
#include "blake2/blake2b-ref.c"
#endif
