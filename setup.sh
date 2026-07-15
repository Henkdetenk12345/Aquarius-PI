#!/bin/bash
set -e

echo "Piquarius Setup for Raspberry Pi 5"
echo "===================================="
echo ""

echo "Updating package list..."
sudo apt update

echo "Installing dependencies..."
sudo apt install -y \
    xvfb \
    ffmpeg \
    vlc \
    chromium-browser \
    python3 \
    python3-pip \
    xdotool \
    pulseaudio \
    pulseaudio-utils \
    x11-utils \
    unclutter \
    openbox

echo "Creating systemd service files..."

sudo tee /etc/systemd/system/aquarius-xvfb.service > /dev/null <<EOF
[Unit]
Description=Aquarius Xvfb Virtual Display
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/aquarius.service > /dev/null <<EOF
[Unit]
Description=Aquarius Playout System
After=aquarius-xvfb.service
Requires=aquarius-xvfb.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
Environment=DISPLAY=:99
ExecStart=/usr/bin/python3 aquarius.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd..."
sudo systemctl daemon-reload

echo "Enabling services..."
sudo systemctl enable aquarius-xvfb.service
sudo systemctl enable aquarius.service

echo ""
echo "Setup complete!"
echo ""
echo "To start manually:"
echo "  sudo systemctl start aquarius-xvfb"
echo "  python3 aquarius.py"
echo ""
echo "To start as services:"
echo "  sudo systemctl start aquarius-xvfb"
echo "  sudo systemctl start aquarius"
echo ""
echo "Edit aquarius_config.json before starting!"