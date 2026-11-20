"""RDT Connect frozen-backend multiprocessing guard.

This user runtime hook runs before PyInstaller's installed runtime hooks.
The desktop backend is intentionally single-process and does not use
multiprocessing shared-memory/semaphore workers.
"""
import sys as _sys

if getattr(_sys, "frozen", False):
    # Never enter PyInstaller's resource-tracker helper dispatch.
    try:
        _rdt_c_index = _sys.argv.index("-c")
    except ValueError:
        _rdt_c_index = -1

    if _rdt_c_index >= 0 and _rdt_c_index + 1 < len(_sys.argv):
        _rdt_command = _sys.argv[_rdt_c_index + 1]
        if _rdt_command.startswith(
            "from multiprocessing.resource_tracker import main"
        ):
            raise SystemExit(0)

    # Prevent the parent process from launching that helper in the first place.
    import multiprocessing.resource_tracker as _rdt_resource_tracker

    def _rdt_disable_resource_tracking(*_args, **_kwargs):
        return None

    _rdt_resource_tracker.register = _rdt_disable_resource_tracking
    _rdt_resource_tracker.unregister = _rdt_disable_resource_tracking

    _rdt_tracker = _rdt_resource_tracker._resource_tracker
    _rdt_tracker.register = _rdt_disable_resource_tracking
    _rdt_tracker.unregister = _rdt_disable_resource_tracking
    _rdt_tracker.ensure_running = _rdt_disable_resource_tracking
