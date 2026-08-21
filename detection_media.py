#!/usr/bin/env python3
"""
detection_media.py — per-detection media for the rms-web night pages.

Run under the RMS venv (~/vRMS/bin/python3) by rms_web.py; the web server itself
stays stdlib-only. Reads the night's canonical FTPdetectinfo + the archived FF
files and writes, into a cache directory:

  index mode:  detections.json + a cropped thumbnail per detection
  full mode:   full-resolution stretched maxpixel JPEG for one detection's FF
  video mode:  a short MP4 of one detection, reconstructed from the FF block

The "video" is the classic binViewer reconstruction: an FF block stores, per
pixel, the max value over 256 frames and WHICH frame it occurred on (maxframe),
so frame i can be rebuilt as avepixel with maxpixel painted where maxframe==i.
That replays transients (the meteor sweeping through) even though the full
25 fps video was never kept. Clips are trimmed to the detection's frame span
(±PAD frames) and encoded at half speed (12.5 fps) — a 5-frame meteor is a
single blink at real time.

Usage:
  detection_media.py index <night_dir> <cache_dir>
  detection_media.py full  <night_dir> <cache_dir> <det_index>
  detection_media.py video <night_dir> <cache_dir> <det_index>
"""
import json
import os
import subprocess
import sys
from datetime import timedelta

# Editable-install RMS resolves from anywhere, but be explicit to dodge the
# ~/source namespace-dir shadowing gotcha (see rms_ml_crops.py).
sys.path.insert(0, os.path.expanduser("~/source/RMS"))

import numpy as np
from PIL import Image

from RMS.Formats.FFfile import read as readFF, filenameToDatetime
from RMS.Formats.FTPdetectinfo import readFTPdetectinfo

PAD_FRAMES = 15       # context frames either side of the detection in the clip
VIDEO_FPS = 12.5      # half real speed so short meteors are visible
THUMB_H = 160         # thumbnail height, px
THUMB_MARGIN = 60     # crop margin around the detection's pixel track


def findFTPdetectinfo(night_dir):
    """The canonical FTPdetectinfo only — never _unfiltered/_uncalibrated/backups."""
    name = "FTPdetectinfo_{}.txt".format(os.path.basename(os.path.normpath(night_dir)))
    path = os.path.join(night_dir, name)
    return path if os.path.isfile(path) else None


def stretchLUT(ff):
    """One linear stretch per FF, shared by thumb/full/video frames so they agree.
    Anchored on the avepixel floor (raw-pipeline nights sit at ~1-3 ADU) with a
    guaranteed span so a dark, clean frame doesn't amplify to pure noise."""
    lo = float(np.percentile(ff.avepixel, 1.0))
    hi = max(float(np.percentile(ff.maxpixel, 99.8)), lo + 40.0)
    lut = np.clip((np.arange(256, dtype=np.float32) - lo)*255.0/(hi - lo), 0, 255)
    return lut.astype(np.uint8)


def loadDetections(night_dir):
    """FTPdetectinfo entries flattened to one dict per detection, index-stable."""
    src = findFTPdetectinfo(night_dir)
    if src is None:
        return None, []
    meteor_list = readFTPdetectinfo(night_dir, os.path.basename(src))
    dets = []
    for entry in meteor_list:
        ff_name, _cam, meteor_no, n_segments, fps, _hnr, _mle, _binn, _pxfm, _rho, _phi, meas = entry
        fps = fps or 25.0
        frames = [m[1] for m in meas]
        xs = [m[2] for m in meas]
        ys = [m[3] for m in meas]
        fmin, fmax = min(frames), max(frames)
        t = filenameToDatetime(ff_name) + timedelta(seconds=fmin/fps)
        i = len(dets)
        dets.append({
            "i": i,
            "ff": ff_name,
            "no": int(meteor_no),
            "time": t.strftime("%H:%M:%S.%f")[:-4] + " UT",
            "date": t.strftime("%Y-%m-%d"),
            "dur": round((fmax - fmin)/fps, 2),
            "nseg": int(n_segments),
            "fmin": fmin,
            "fmax": fmax,
            "fps": fps,
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "has_ff": os.path.isfile(os.path.join(night_dir, ff_name)),
            "thumb": "det{:03d}_thumb.jpg".format(i),
            "video": "det{:03d}.mp4".format(i),
            "full": "det{:03d}_full.jpg".format(i),
        })
    return src, dets


def readFFcached(night_dir, ff_name, cache={}):
    """FFs repeat across detections (several meteors per block); read each once."""
    if ff_name not in cache:
        cache[ff_name] = readFF(night_dir, ff_name)
    return cache[ff_name]


def writeIndex(night_dir, cache_dir):
    src, dets = loadDetections(night_dir)
    os.makedirs(cache_dir, exist_ok=True)
    for d in dets:
        if not d["has_ff"]:
            continue
        ff = readFFcached(night_dir, d["ff"])
        lut = stretchLUT(ff)
        h, w = ff.maxpixel.shape
        x0, y0, x1, y1 = d["bbox"]
        x0 = max(0, int(x0) - THUMB_MARGIN); y0 = max(0, int(y0) - THUMB_MARGIN)
        x1 = min(w, int(x1) + THUMB_MARGIN); y1 = min(h, int(y1) + THUMB_MARGIN)
        crop = lut[ff.maxpixel[y0:y1, x0:x1]]
        img = Image.fromarray(crop)
        img = img.resize((max(1, int(img.width*THUMB_H/img.height)), THUMB_H), Image.LANCZOS)
        img.save(os.path.join(cache_dir, d["thumb"]), quality=88)
    out = {
        "version": 1,
        "night": os.path.basename(os.path.normpath(night_dir)),
        "src": os.path.basename(src) if src else None,
        "src_mtime": int(os.path.getmtime(src)) if src else 0,
        "detections": dets,
    }
    tmp = os.path.join(cache_dir, "detections.json.tmp")
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, os.path.join(cache_dir, "detections.json"))


def loadIndexEntry(cache_dir, det_index):
    with open(os.path.join(cache_dir, "detections.json")) as f:
        idx = json.load(f)
    return idx["detections"][det_index]


def writeFull(night_dir, cache_dir, det_index):
    d = loadIndexEntry(cache_dir, det_index)
    ff = readFF(night_dir, d["ff"])
    lut = stretchLUT(ff)
    Image.fromarray(lut[ff.maxpixel]).save(os.path.join(cache_dir, d["full"]), quality=90)


def writeVideo(night_dir, cache_dir, det_index):
    d = loadIndexEntry(cache_dir, det_index)
    ff = readFF(night_dir, d["ff"])
    lut = stretchLUT(ff)
    n = ff.nframes if getattr(ff, "nframes", 0) else 256
    f0 = max(0, int(d["fmin"]) - PAD_FRAMES)
    f1 = min(n - 1, int(np.ceil(d["fmax"])) + PAD_FRAMES)
    h, w = ff.maxpixel.shape
    out = os.path.join(cache_dir, d["video"])
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "gray",
           "-video_size", "{}x{}".format(w, h), "-framerate", str(VIDEO_FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out + ".tmp.mp4"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(f0, f1 + 1):
        frame = ff.avepixel.copy()
        m = ff.maxframe == i
        frame[m] = ff.maxpixel[m]
        proc.stdin.write(lut[frame].tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed for {}".format(out))
    os.replace(out + ".tmp.mp4", out)


if __name__ == "__main__":
    mode, night_dir, cache_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "index":
        writeIndex(night_dir, cache_dir)
    elif mode == "full":
        writeFull(night_dir, cache_dir, int(sys.argv[4]))
    elif mode == "video":
        writeVideo(night_dir, cache_dir, int(sys.argv[4]))
    else:
        sys.exit("unknown mode: " + mode)
