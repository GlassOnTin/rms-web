#!/usr/bin/env bash
# install.sh — configure and enable rms-web systemd units for a GMN/RMS station.
#
# Standard GMN Raspberry Pi / Linux single-station install:
#   ./install.sh
#
# Second camera instance on the same host:
#   ./install.sh --instance cam2 --rms-source ~/source/RMS-cam2 --venv ~/vRMS-cam2 --port 8081
#
# Non-root preview or staged generation:
#   ./install.sh --dry-run
#   ./install.sh --stage-dir /tmp/rms-web-stage
#
# Remove installed units:
#   ./install.sh --uninstall [--instance cam2]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTANCE=""
PORT=""
RMS_SRC=""
RMS_CFG=""
RMS_DATA_OVERRIDE=""
VENV_DIR=""
CAPTURE_SVC=""
ENABLE_CALIB="0"
UNINSTALL="0"
DRY_RUN="0"
STAGE_DIR=""
SKIP_PIP="0"
SKIP_FETCH="0"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --user <user>             Account running RMS (default: $SUDO_USER or $USER)
  --instance <name>         Optional 2nd-camera suffix (creates rms-web-<name>.service)
  --port <port>             HTTP listen port (default: 8080)
  --rms-source <dir>        Path to RMS git clone (default: ~<user>/source/RMS)
  --rms-config <file>       Path to RMS .config (default: <rms-source>/.config)
  --rms-data <dir>          Override RMS_data path (default: read data_dir from .config)
  --venv <dir>              Path to vRMS virtualenv (default: ~<user>/vRMS)
  --capture-service <name>  systemd capture unit name if used (default: rms-capture[-<instance>])
  --enable-calib            Enable experimental /tonight platepar solve/refit/bump UI buttons
  --skip-pip                Do not install missing matplotlib/pillow into vRMS
  --skip-initial-fetch      Skip initial neighbours map & scorecard build
  --dry-run                 Print generated config and units without writing or enabling
  --stage-dir <dir>         Write generated env + unit files into <dir> without sudo/systemctl
  --uninstall               Stop, disable, and remove systemd units for this instance
  -h, --help                Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)            RUN_USER="$2"; shift 2 ;;
        --instance)        INSTANCE="$2"; shift 2 ;;
        --port)            PORT="$2"; shift 2 ;;
        --rms-source)      RMS_SRC="$2"; shift 2 ;;
        --rms-config)      RMS_CFG="$2"; shift 2 ;;
        --rms-data)        RMS_DATA_OVERRIDE="$2"; shift 2 ;;
        --venv)            VENV_DIR="$2"; shift 2 ;;
        --capture-service) CAPTURE_SVC="$2"; shift 2 ;;
        --enable-calib)    ENABLE_CALIB="1"; shift ;;
        --skip-pip)        SKIP_PIP="1"; shift ;;
        --skip-initial-fetch) SKIP_FETCH="1"; shift ;;
        --dry-run)         DRY_RUN="1"; shift ;;
        --stage-dir)       STAGE_DIR="$2"; shift 2 ;;
        --uninstall)       UNINSTALL="1"; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [[ -n "$INSTANCE" && ! "$INSTANCE" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "Error: --instance must contain only letters, digits, '_' or '-'." >&2
    exit 1
fi

# Resolve target user's home and primary group
if [[ -n "$STAGE_DIR" && "$RUN_USER" != "$(id -un)" ]] && ! id "$RUN_USER" >/dev/null 2>&1; then
    USER_HOME="$HOME"
    RUN_GROUP="$RUN_USER"
else
    USER_HOME="$(eval echo "~$RUN_USER")"
    if [[ ! -d "$USER_HOME" ]]; then
        USER_HOME="$HOME"
    fi
    RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"
fi

SFX="${INSTANCE:+-$INSTANCE}"
DESC_SUFFIX="${INSTANCE:+ ($INSTANCE)}"
ENV_NAME="rms-web${SFX}.env"
WEB_UNIT="rms-web${SFX}.service"
PREV_UNIT="rms-tonight-preview${SFX}.service"
NEIGH_SVC="rms-neighbours-refresh${SFX}.service"
NEIGH_TMR="rms-neighbours-refresh${SFX}.timer"
SCORE_SVC="rms-scorecard-refresh${SFX}.service"
SCORE_TMR="rms-scorecard-refresh${SFX}.timer"

SUDO=""
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

if [[ "$UNINSTALL" == "1" ]]; then
    echo "Removing rms-web units${DESC_SUFFIX}..."
    for u in "$WEB_UNIT" "$PREV_UNIT" "$NEIGH_TMR" "$NEIGH_SVC" "$SCORE_TMR" "$SCORE_SVC"; do
        $SUDO systemctl disable --now "$u" 2>/dev/null || true
        $SUDO rm -f "/etc/systemd/system/$u"
    done
    $SUDO rm -f "/etc/rms-web/$ENV_NAME"
    $SUDO systemctl daemon-reload
    echo "Uninstalled $WEB_UNIT."
    exit 0
fi

RMS_SRC="${RMS_SRC:-$USER_HOME/source/RMS}"
RMS_CFG="${RMS_CFG:-$RMS_SRC/.config}"
VENV_DIR="${VENV_DIR:-$USER_HOME/vRMS}"
PORT="${PORT:-8080}"
CAPTURE_SVC="${CAPTURE_SVC:-rms-capture${SFX}}"

if [[ ! -f "$RMS_CFG" ]]; then
    echo "Error: RMS config not found at $RMS_CFG" >&2
    echo "Pass --rms-source <dir> or --rms-config <path>." >&2
    exit 1
fi

# Parse key from RMS .config (stripping ';' comments and expanding ~)
cfg_get() {
    local key="$1" val
    val="$(awk -F: -v k="$key" '{
        sub(/;.*/, "");
        gsub(/^[ \t]+|[ \t]+$/, "", $1);
        if ($1 == k) {
            sub(/^[^:]*:/, "");
            gsub(/^[ \t]+|[ \t]+$/, "");
            print;
            exit;
        }
    }' "$RMS_CFG")"
    if [[ "$val" == "~/"* ]]; then
        val="$USER_HOME/${val:2}"
    elif [[ "$val" == "~" ]]; then
        val="$USER_HOME"
    fi
    printf '%s' "$val"
}

STATION_ID="$(cfg_get stationID)"
CFG_DATA_DIR="$(cfg_get data_dir)"
RMS_DATA_DIR="${RMS_DATA_OVERRIDE:-${CFG_DATA_DIR:-$USER_HOME/RMS_data}}"
PLATEPAR_PATH="$RMS_SRC/platepar_cmn2010.cal"

VENV_PY=""
for cand in "$VENV_DIR/bin/python3" "$VENV_DIR/bin/python"; do
    if [[ -x "$cand" ]]; then
        VENV_PY="$cand"
        break
    fi
done
if [[ -z "$VENV_PY" ]]; then
    echo "Error: RMS virtualenv python not found under $VENV_DIR/bin/" >&2
    echo "Pass --venv <path-to-vRMS>." >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: 'ffmpeg' not found on PATH; per-detection MP4 clips require ffmpeg (sudo apt install ffmpeg)." >&2
fi

MOUNT_LINE="# Local filesystem (no extra mount dependency)"
if [[ "$RMS_DATA_DIR" == /mnt/* || "$RMS_DATA_DIR" == /media/* ]]; then
    MOUNT_LINE="RequiresMountsFor=$RMS_DATA_DIR"
fi

if [[ -n "$STAGE_DIR" ]]; then
    ENV_DEST="$STAGE_DIR/env/$ENV_NAME"
    UNIT_DIR="$STAGE_DIR/systemd"
else
    ENV_DEST="/etc/rms-web/$ENV_NAME"
    UNIT_DIR="/etc/systemd/system"
fi

ENV_CONTENT="# Generated by rms-web/install.sh
RMS_DATA=$RMS_DATA_DIR
RMS_WEB_PORT=$PORT
RMS_SOURCE=$RMS_SRC
RMS_CONFIG=$RMS_CFG
RMS_PLATEPAR=$PLATEPAR_PATH
RMS_WEB_VENV_PY=$VENV_PY
RMS_CAPTURE_SERVICE=$CAPTURE_SVC
RMS_WEB_CALIB=$ENABLE_CALIB
"

render_unit() {
    local tpl="$1"
    sed \
        -e "s|@USER@|$RUN_USER|g" \
        -e "s|@GROUP@|$RUN_GROUP|g" \
        -e "s|@REPO_DIR@|$REPO_DIR|g" \
        -e "s|@VENV_PY@|$VENV_PY|g" \
        -e "s|@ENV_FILE@|$ENV_DEST|g" \
        -e "s|@CAPTURE_SERVICE@|$CAPTURE_SVC|g" \
        -e "s|@DESC_SUFFIX@|$DESC_SUFFIX|g" \
        -e "s|@MOUNT_LINE@|$MOUNT_LINE|g" \
        "$REPO_DIR/deploy/$tpl"
}

render_timer() {
    local tpl="$1" svc_unit="$2"
    sed "/^\[Timer\]/a Unit=$svc_unit" "$REPO_DIR/deploy/$tpl"
}

if [[ "$DRY_RUN" == "1" ]]; then
    echo "=== Environment file ($ENV_DEST) ==="
    printf '%s\n' "$ENV_CONTENT"
    echo "=== $WEB_UNIT ==="
    render_unit "rms-web.service"
    echo "=== $PREV_UNIT ==="
    render_unit "rms-tonight-preview.service"
    exit 0
fi

if [[ "$SKIP_PIP" == "0" ]]; then
    if ! "$VENV_PY" -c "import matplotlib, PIL" >/dev/null 2>&1; then
        echo "Installing matplotlib and pillow into $VENV_DIR..."
        "$VENV_PY" -m pip install -q -r "$REPO_DIR/requirements.txt"
    fi
fi

if [[ -n "$STAGE_DIR" ]]; then
    mkdir -p "$(dirname "$ENV_DEST")" "$UNIT_DIR"
    printf '%s' "$ENV_CONTENT" > "$ENV_DEST"
    render_unit "rms-web.service"                > "$UNIT_DIR/$WEB_UNIT"
    render_unit "rms-tonight-preview.service"    > "$UNIT_DIR/$PREV_UNIT"
    render_unit "rms-neighbours-refresh.service" > "$UNIT_DIR/$NEIGH_SVC"
    render_unit "rms-scorecard-refresh.service"  > "$UNIT_DIR/$SCORE_SVC"
    render_timer "rms-neighbours-refresh.timer" "$NEIGH_SVC" > "$UNIT_DIR/$NEIGH_TMR"
    render_timer "rms-scorecard-refresh.timer"  "$SCORE_SVC" > "$UNIT_DIR/$SCORE_TMR"
    echo "Staged config and units under $STAGE_DIR (station=${STATION_ID:-RMS}, data=$RMS_DATA_DIR, port=$PORT)."
    exit 0
fi

echo "Installing rms-web${DESC_SUFFIX} for station ${STATION_ID:-RMS} (data: $RMS_DATA_DIR, port: $PORT)..."
$SUDO mkdir -p /etc/rms-web
printf '%s' "$ENV_CONTENT" | $SUDO tee "$ENV_DEST" >/dev/null
render_unit "rms-web.service"                | $SUDO tee "$UNIT_DIR/$WEB_UNIT" >/dev/null
render_unit "rms-tonight-preview.service"    | $SUDO tee "$UNIT_DIR/$PREV_UNIT" >/dev/null
render_unit "rms-neighbours-refresh.service" | $SUDO tee "$UNIT_DIR/$NEIGH_SVC" >/dev/null
render_unit "rms-scorecard-refresh.service"  | $SUDO tee "$UNIT_DIR/$SCORE_SVC" >/dev/null
render_timer "rms-neighbours-refresh.timer" "$NEIGH_SVC" | $SUDO tee "$UNIT_DIR/$NEIGH_TMR" >/dev/null
render_timer "rms-scorecard-refresh.timer"  "$SCORE_SVC" | $SUDO tee "$UNIT_DIR/$SCORE_TMR" >/dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$WEB_UNIT" "$PREV_UNIT" "$NEIGH_TMR" "$SCORE_TMR"
$SUDO systemctl restart "$WEB_UNIT" "$PREV_UNIT"

if [[ "$SKIP_FETCH" == "0" ]]; then
    if [[ ! -f "$REPO_DIR/neighbours.json" ]]; then
        echo "Building initial neighbours map..."
        RMS_CONFIG="$RMS_CFG" RMS_DATA="$RMS_DATA_DIR" "$VENV_PY" "$REPO_DIR/make_neighbours_map.py" --refresh || true
    fi
    if [[ ! -f "$REPO_DIR/scorecard.json" ]]; then
        echo "Building initial network scorecard..."
        RMS_CONFIG="$RMS_CFG" RMS_DATA="$RMS_DATA_DIR" /usr/bin/python3 "$REPO_DIR/make_scorecard.py" || true
    fi
fi

HOST_LABEL="$(hostname -f 2>/dev/null || hostname)"
echo "Done. Dashboard running at: http://${HOST_LABEL}:${PORT}/"
