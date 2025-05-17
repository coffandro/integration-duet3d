#!/usr/bin/env bash
# install_mjpg_streamer.sh
# ------------------------
# Clone, build & install MJPG-Streamer under /opt,
# prompt to pick a /dev/video* device,
# auto-detect its resolution and frame-rate (or let you choose),
# and configure a systemd service called 'mjpeg-streamer'.

set -euo pipefail

# 1) Ensure root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo $0" >&2
  exit 1
fi

# 2) Install build dependencies
apt-get update
apt-get install -y \
  git build-essential cmake \
  libjpeg62-turbo-dev libv4l-dev v4l-utils pkg-config

# 3) Clone & compile MJPG-Streamer
rm -rf /opt/mjpg-streamer
git clone https://github.com/jacksonliam/mjpg-streamer.git /opt/mjpg-streamer
cd /opt/mjpg-streamer/mjpg-streamer-experimental
make
rm -rf /opt/mjpg-streamer/.git

# 4) List all /dev/video* and prompt user to select
echo "Available video devices:"
DEVICES=( $(ls /dev/video*) )
select DEV in "${DEVICES[@]}"; do
  if [[ -n "$DEV" ]]; then
    CAMERA_DEV="$DEV"
    break
  else
    echo "Invalid selection. Please try again."
  fi
done
echo "→ You selected: $CAMERA_DEV"

# 5) Auto-detect resolution (width×height) via v4l2-ctl
if command -v v4l2-ctl &>/dev/null; then
  RES=$(v4l2-ctl --device="$CAMERA_DEV" --list-formats-ext \
        | grep -o '[0-9]\+x[0-9]\+' | head -n1)
fi

# Fallback to 640x480 if detection failed
if [[ -z "${RES:-}" ]]; then
  echo "⚠️  Could not detect resolution; defaulting to 640x480"
  RES="640x480"
else
  echo "Detected native resolution: $RES"
fi

# 5a) Detect supported frame rates for that resolution
FRAMERATE=""
if command -v v4l2-ctl &>/dev/null; then
  echo "Detecting supported frame‐rates at ${RES}..."
  # grab the list of "Discrete" intervals for that resolution block
  mapfile -t INTERVALS < <(v4l2-ctl --device="$CAMERA_DEV" --list-formats-ext \
    | awk -v res="$RES" '
        $0 ~ "Size: " res { inblock=1; next }
        inblock && /Interval: Discrete/ {
          gsub(/[^0-9\/]/,"",$0);
          print $0
        }
        inblock && /^$/ { inblock=0 }
      ')
  # convert to fps (fps = denominator / numerator)
  declare -A FPS_SET=()
  for I in "${INTERVALS[@]}"; do
    num=${I%%/*}
    den=${I##*/}
    # compute float fps, then round to nearest integer
    fps=$(awk -v n="$num" -v d="$den" 'BEGIN { printf("%.0f", d/n) }')
    FPS_SET[$fps]=1
  done

  if (( ${#FPS_SET[@]} > 0 )); then
    # present the sorted unique list
    FR_OPTIONS=( $(printf "%s\n" "${!FPS_SET[@]}" | sort -n) )
    echo "Supported rates: ${FR_OPTIONS[*]} fps"
    echo "Select frame rate:"
    select F in "${FR_OPTIONS[@]}"; do
      if [[ -n "$F" ]]; then
        FRAMERATE="$F"
        break
      else
        echo "Invalid selection. Please try again."
      fi
    done
  fi
fi

# 5b) Fallback to manual selection if detection failed
if [[ -z "$FRAMERATE" ]]; then
  echo "Could not auto-detect frame rates; please choose one:"
  MANUAL=(24 30 60)
  select FR in "${MANUAL[@]}"; do
    if [[ -n "$FR" ]]; then
      FRAMERATE="$FR"
      break
    else
      echo "Invalid choice."
    fi
  done
fi
echo "→ Using frame rate: ${FRAMERATE} fps"

# 5c) Detect MJPG support
if v4l2-ctl --device="$CAMERA_DEV" --list-formats 2>/dev/null \
     | grep -q MJPG; then
  PIX_OPTS=""
  echo "✔ Camera supports MJPG"
else
  PIX_OPTS="-y"
  echo "⚠ Camera does NOT support MJPG; will stream YUV (-y)"
fi

# 6) Drop systemd unit, injecting $PIX_OPTS and chosen framerate
cat <<EOF >/etc/systemd/system/mjpeg-streamer.service
[Unit]
Description=MJPG-Streamer Service
After=network-online.target

[Service]
ExecStartPre=/sbin/modprobe uvcvideo
ExecStart=/opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer \\
  -i "input_uvc.so -d ${CAMERA_DEV} -r ${RES} -f ${FRAMERATE} ${PIX_OPTS}" \\
  -o "output_http.so -w /opt/mjpg-streamer/mjpg-streamer-experimental/www -p 8080"
Restart=always
User=root
Group=root
WorkingDirectory=/opt/mjpg-streamer/mjpg-streamer-experimental

[Install]
WantedBy=multi-user.target
EOF

# 7) Clean up & start service
apt-get clean
rm -rf /var/lib/apt/lists/*

systemctl daemon-reload
systemctl enable mjpeg-streamer
systemctl restart mjpeg-streamer

echo
echo "✅ mjpg-streamer is up and running:"
echo "   • Device:     $CAMERA_DEV"
echo "   • Resolution: $RES"
echo "   • Frame rate: ${FRAMERATE} fps"
echo "   • Stream:     http://<this_host_ip>:8080/?action=stream"
echo "   • Snapshot:   http://<this_host_ip>:8080/?action=snapshot"