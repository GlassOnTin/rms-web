#!/usr/bin/env bash
# rms-csi-bridge.sh — CSI camera → raw V4L2 loopback bridge for RMS on Raspberry Pi 5
#
# RAW PIPELINE (2026-08-21). The old H.264 path (rpicam-vid x264 → RTSP →
# MediaMTX → RMS GStreamer decode) was costing ~2 magnitudes of meteor
# sensitivity: the software x264 encode injected a heavy noise tail into the
# stream (FF maxpixel noise floor ~26 ADU vs ~1 ADU physical), which forced the
# detection threshold to j1=30 (~40 ADU ≈ meteor LM +1 vs neighbours' +3).
# Measured 2026-08-21; see memory project-meteor-sensitivity-analysis. The fix
# is to keep the encoder out of the detection path entirely:
#
#   rpicam-vid (--codec yuv420, raw) --pipe--> ffmpeg (crop stride, Y plane only)
#                                    --> /dev/video10 (v4l2loopback "rms-raw", GREY)
#                                    --> RMS (media_backend v4l2, cv2 CAP_V4L2)
#
# STRIDE: rpicam-vid's yuv420 output pads each line to a 1536-byte stride at
# 1456 wide (re-verified 2026-08-21: 3 s capture = exactly 60.000 frames at
# stride 1536; frame rendered and eyeballed — no shear). ffmpeg MUST read
# -video_size 1536x1088 and crop back to 1456, else every line misreads and
# produces sheared garbage whose global stats still look normal.
#
# GREY: only the Y plane goes to the loopback (format=gray = plane pick, no
# math). cv2 CAP_V4L2 with CONVERT_RGB=0 then delivers (1088,1456) uint8
# frames straight into RMS — zero conversions, zero encode CPU, no rate-control
# blowout failure mode (the 2026-08-20 164 Mbps incident class is gone).
#
# WEB VIEW: no longer produced here. MediaMTX serves it ON-DEMAND (mediamtx.yml
# path cam1 runOnDemand encodes /dev/video10 at reduced fps/size only while a
# viewer is connected): WebRTC http://<host>:8889/cam1
#
# Usage:
#   ./rms-csi-bridge.sh          # interactive: prints URLs, supervises (restart on failure)
#   ./rms-csi-bridge.sh --once   # single pipeline run, no loop
#                                # (for systemd: let the unit handle ordering + Restart=always)
set -u
set -o pipefail

# --- config -----------------------------------------------------------------
WIDTH=${WIDTH:-1456}          # IMX296 native (square pixels — good for astrometry)
HEIGHT=${HEIGHT:-1088}
STRIDE=${STRIDE:-1536}        # rpicam yuv420 line stride at 1456 wide (measured — see header)
FPS=${FPS:-25}
# --- exposure: FIXED (manual) — never auto for a meteor station -------------
# Auto AEC/AGC hunts frame-to-frame → fluctuating background → false detections
# and uncalibratable photometry. Lock shutter to ~1 frame period (max light per
# frame at 25 fps) and gain to a fixed value tuned on-sky.
SHUTTER=${SHUTTER:-40000}     # microseconds (~1/25 s). Must be <= 1e6/FPS.
GAIN=${GAIN:-96}              # analogue gain. IMX296 analogue tops out ~register 239 = --gain ~96
                              # (measured 2026-08-18); higher --gain is digital noise only. 96 = analogue max.
TUNING_FILE=${TUNING_FILE:-}  # empty = STOCK system tuning. A linear-tone-curve/pedestal custom tuning
                              # KILLS detection here (0 vs 84 stars, measured 2026-08-18) — the stock
                              # non-linear shadow-lift is essential for faint stars. Leave empty.
LOOPBACK_DEV=${LOOPBACK_DEV:-/dev/video10}  # v4l2loopback "rms-raw" (module opts in /etc/modprobe.d/v4l2loopback.conf)
WEB_LOOPBACK_DEV=${WEB_LOOPBACK_DEV:-/dev/video11}  # 2nd loopback for the on-demand web view: RMS holds
                              # $LOOPBACK_DEV exclusively during capture, so the web encoder can't share it.
                              # The bridge tees raw frames here too; mediamtx cam1 runOnDemand reads this one.
                              # If absent, the bridge silently falls back to RMS-only (no web view).
ONCE=0; [ "${1:-}" = "--once" ] && ONCE=1
# ----------------------------------------------------------------------------

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# One camera->loopback pipeline run (blocks until it exits). rpicam-vid emits
# raw stride-padded YUV420; ffmpeg reads at the padded geometry, crops back to
# the true width, keeps only the luma plane, and writes GREY frames to the
# v4l2loopback device that RMS reads.
run_pipeline() {
    if [ ! -e "$LOOPBACK_DEV" ]; then
        log "ERROR: $LOOPBACK_DEV missing — v4l2loopback module not loaded?"
        return 1
    fi
    # Build ffmpeg outputs: always feed RMS on $LOOPBACK_DEV; if the web loopback
    # exists, split and also feed it (raw copy, ~free) so the on-demand web view
    # has its own reader. The split keeps a single decode; only the extra memcpy
    # to the 2nd device is added (no encode here — that's on-demand in mediamtx).
    local ff_out
    if [ -e "$WEB_LOOPBACK_DEV" ]; then
        log "camera->raw ${WIDTH}x${HEIGHT}@${FPS} (GREY) -> ${LOOPBACK_DEV} (RMS) + ${WEB_LOOPBACK_DEV} (web)"
        ff_out=(-filter_complex "[0:v]crop=${WIDTH}:${HEIGHT}:0:0,format=gray,split=2[rms][web]"
                -map "[rms]" -f v4l2 -pix_fmt gray "$LOOPBACK_DEV"
                -map "[web]" -f v4l2 -pix_fmt gray "$WEB_LOOPBACK_DEV")
    else
        log "camera->raw ${WIDTH}x${HEIGHT}@${FPS} (GREY, stride ${STRIDE}) -> ${LOOPBACK_DEV} (RMS only)"
        ff_out=(-vf "crop=${WIDTH}:${HEIGHT}:0:0,format=gray"
                -f v4l2 -pix_fmt gray "$LOOPBACK_DEV")
    fi
    rpicam-vid -n -t 0 --width "$WIDTH" --height "$HEIGHT" --framerate "$FPS" \
               --shutter "$SHUTTER" --gain "$GAIN" ${TUNING_FILE:+--tuning-file "$TUNING_FILE"} --denoise off --sharpness 0 \
               --codec yuv420 -o - 2>/tmp/rms-csi-rpicam.log \
      | ffmpeg -hide_banner -loglevel warning -fflags nobuffer \
               -f rawvideo -pixel_format yuv420p -video_size "${STRIDE}x${HEIGHT}" -framerate "$FPS" -i - \
               "${ff_out[@]}" 2>/tmp/rms-csi-ffmpeg.log
}

cleanup() { log "stopping bridge"; pkill -x rpicam-vid 2>/dev/null; pkill -x ffmpeg 2>/dev/null; exit 0; }
trap cleanup INT TERM

if [ "$ONCE" = "1" ]; then
    # systemd mode: run once; let the unit's Restart=always resurrect it. On exit,
    # make sure the rpicam half of the pipe doesn't linger (systemd cgroup kill also covers this).
    run_pipeline; rc=$?
    pkill -x rpicam-vid 2>/dev/null
    exit "$rc"
fi

# Interactive mode ------------------------------------------------------------
IP=$(hostname -I | awk '{print $1}')
log "RMS raw feed:               ${LOOPBACK_DEV} (GREY ${WIDTH}x${HEIGHT}@${FPS})"
log "live view (on-demand):      WebRTC http://${IP}:8889/cam1  (or http://$(hostname).local:8889/cam1)"

# Supervise: if the pipeline dies (camera hiccup), restart it.
while true; do
    run_pipeline
    log "pipeline exited (rc=$?) — restarting in 3s"
    pkill -x rpicam-vid 2>/dev/null
    sleep 3
done
