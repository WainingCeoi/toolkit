"""Stop and reap children started in private process groups."""

import os
import signal
import time


def group_exists(process):
    process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can return EPERM while the group's final children exit.
        pass
    return True


def stop_groups(processes, *, grace=3):
    remaining = [process for process in processes if group_exists(process)]
    for signum, timeout in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        if not remaining:
            return
        for process in remaining:
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError):
                # Verify the group is gone below, including during exit races.
                pass
        deadline = time.monotonic() + timeout
        while remaining and time.monotonic() < deadline:
            remaining = [process for process in remaining if group_exists(process)]
            if remaining:
                time.sleep(0.05)
    if remaining:
        pids = ", ".join(str(process.pid) for process in remaining)
        raise RuntimeError(f"Could not stop owned process groups {pids}.")
