"""Exit codes shared by every command."""

OK = 0
USAGE = 2
NOT_SUPPORTED = 3
CREDENTIALS = 4
QUOTE_INCOMPLETE = 5
BOOTSTRAP_FAILED = 6
CLEANUP_PENDING = 7
CAP_REACHED = 8
WORKLOAD_REJECTED = 9

# Used only for a catalog file that fails validation. It is not one of the
# documented run outcomes above.
INVALID_FILE = 1

# A command whose code has not been written yet exits with the usage code.
NOT_IMPLEMENTED = USAGE
