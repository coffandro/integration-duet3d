Meltingplot Duet <-> Simplyprint connector
==========================================

This package provides a connector between a Duet based 3D printer and the
Simplyprint.io cloud service.

The communication is using the Duet HTTP API.
See https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests for more details.


------------
Status
------------
This package is in an early stage of development.
It is not yet feature complete and may contain bugs.

Supported features:

- Printer registration
- Printer status update
- Webcam snapshot livestream
- GCode receiving
- File downloading
- Printer control (start, pause, resume, cancel)

Missing features:

- GCode Macros / Scripts
- Device healts update
- Bed leveling
- Filament Sensor
- PSU Control
- GCode terminal
- Receive messages from Printer in Simplyprint.io


------------
Installation
------------

.. code-block:: sh

    sudo apt-get install git ffmpeg python3-venv gcc g++ make python3-dev
    cd ~
    mkdir mp_duet_simplyprint_connector
    cd mp_duet_simplyprint_connector
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip setuptools wheel
    pip install simplyprint_ws_client@git+https://github.com/SimplyPrint/printer-ws-client.git@main
    pip install meltingplot.duet_simplyprint_connector
    # see next section for content of config.json
    simplyprint config new
    vi ~/.config/SimplyPrint/DuetConnector.json
    sudo ln -s ~/mp_duet_simplyprint_connector/venv/bin/simplyprint /usr/local/bin/simplyprint
    sudo cp ~/mp_duet_simplyprint_connector/venv/simplyprint-connector.service /etc/systemd/system
    # change service file to match user and group and working dir, e.g. tim tim /home/tim/mp_duet_simplyprint_connector
    sudo vi /etc/systemd/system/simplyprint-connector.service
    sudo systemctl enable simplyprint-connector.service
    sudo systemctl start simplyprint-connector.service

-----------------------------
Content of DuetConnector.json
-----------------------------
.. code-block:: json

    [
        {
            "id": null,
            "token": null,
            "name": null,
            "in_setup": true,
            "short_id": null,
            "public_ip": null,
            "unique_id": "...",
            "duet_uri": "IP_OF_YOUR_DUET",
            "duet_password": "reprap",
            "webcam_uri": "http://URI_OF_WEBCAM_SNAPSHOT_ENDPOINT/snapshot"
        }
    ]


-----------------------------------------------
Usage of Meltingplot Duet Simplyprint Connector
-----------------------------------------------

Create a configuration.
Start the duet simplyprint connector.
Add the printer via the Simplyprint.io web interface.
