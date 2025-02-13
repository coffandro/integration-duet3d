#!/bin/bash
# It is used to install the necessary components for the Meltingplot Duet SimplyPrint Connector.
# Ensure you have the required permissions to execute this script.

set -e

if [ "$(uname -m)" != "aarch64" ]; then
    echo "This script is only for 64-bit systems."
    exit 1
fi

if ! grep -q "bookworm" /etc/os-release; then
    echo "This script is only for Debian Bookworm."
    exit 1
fi

sudo mkdir -p /opt/duet-simplyprint-connector
sudo chown "$USER":"$USER" /opt/duet-simplyprint-connector
sudo chmod 755 /opt/duet-simplyprint-connector

sudo apt-get update
sudo apt-get install -y git ffmpeg python3-venv gcc g++ make python3-dev

cd /opt/duet-simplyprint-connector
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install meltingplot.duet_simplyprint_connector
simplyprint autodiscover
simplyprint install-as-service