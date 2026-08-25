"""
calib_lock.py — one shared mutual-exclusion lock for the calibration scripts
(auto_tilt_solve, auto_refit_distortion, bump_solve).

All three back up and overwrite the SAME live platepar, so at most one may run at
a time. An atomic O_EXCL lock makes rapid or concurrent clicks — of the same
button or different ones, and the 15-min cron firing mid-run — a strict no-op:
the first caller wins, the rest see the lock and exit. A stale lock left by a
crashed run is reclaimed after STALE_S so the buttons can't wedge permanently.

Per-action flags (~/.tilt_solving / .tilt_refitting / .tilt_bumpsolving) remain,
but only as UI state (which action is running / hide the buttons); this lock, not
those flags, is what actually enforces exclusion.
"""
import os, errno, time, datetime

LOCK = "/home/ian/.calib_lock"
STALE_S = 20 * 60


def acquire():
    """Atomically take the shared calibration lock. Return True if held, False if busy."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, datetime.datetime.utcnow().isoformat().encode())
        os.close(fd)
        return True
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        try:
            if time.time() - os.path.getmtime(LOCK) > STALE_S:
                os.remove(LOCK)                 # crashed run — reclaim
                return acquire()
        except OSError:
            pass
        return False


def release():
    try:
        os.remove(LOCK)
    except OSError:
        pass
