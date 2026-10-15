"""Error types. Each carries the process exit code and a stable ``code`` string.

The exit codes are part of the command-line contract (docs/design.md).
"""

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_NOT_FOUND = 4
EXIT_IO = 5
EXIT_MISMATCH = 6
EXIT_BUSY = 7


class XeneonError(Exception):
    """Base class for every error the tool reports on purpose."""

    exit_code = EXIT_UNEXPECTED
    code = "unexpected"


class UsageError(XeneonError):
    """Bad command line or a value outside its range; detected before any I/O."""

    exit_code = EXIT_USAGE
    code = "usage"


class RefusedError(XeneonError):
    """A safety rule refused the command; nothing was sent."""

    exit_code = EXIT_REFUSED
    code = "refused"


class DeviceNotFoundError(XeneonError):
    """The display, its hidraw node or its DDC/CI bus was not found, or is not accessible."""

    exit_code = EXIT_NOT_FOUND
    code = "not_found"


class TransportError(XeneonError):
    """I/O failure or a reply that does not match the protocol."""

    exit_code = EXIT_IO
    code = "io"


class VerifyMismatchError(XeneonError):
    """The value read back differs from the value written."""

    exit_code = EXIT_MISMATCH
    code = "mismatch"


class LockBusyError(XeneonError):
    """Another writer holds the single-writer lock."""

    exit_code = EXIT_BUSY
    code = "busy"
