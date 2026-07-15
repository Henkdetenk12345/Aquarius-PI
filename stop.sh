#!/bin/bash
echo "Stopping Aquarius..."
pkill -9 -f "aquarius.py"
pkill -9 -f "vlc.*rc-unix"
pkill -9 -f "chromium.*kiosk"
pkill -9 -f "chromium.*no-sandbox"
pkill -9 -f "ffmpeg.*flv"
pkill -9 -f Xvfb
pkill -9 -f openbox
pkill -9 -f unclutter
rm -f /tmp/aquarius-vlc-os1.sock /tmp/aquarius-vlc-media.sock /tmp/aquarius-vlc-ident.sock /tmp/aquarius.log
echo "All stopped."
