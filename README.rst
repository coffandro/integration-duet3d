Duet RepRapFirmware to Simplyprint.io connector
================================================

This package acts as a bridge between Duet-based 3D printers and the Simplyprint.io cloud service.

It communicates with the printer using the Duet HTTP API.
For more information, visit https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests.

Communication with Simplyprint.io is handled via the `simplyprint-ws-client`.

------------
Status
------------

Supported features:

- Printer registration
- Printer status update
- Webcam snapshot livestream
- GCode receiving
- File downloading
- Printer control (start, pause, resume, cancel)
- Self Upgrading VIA G-Code M997
- Device healts update
- Bed leveling
- Filament Sensor
- Duet auto discovery with tracking based on BoardID

Missing features:

- PSU Control
- GCode Macros / Scripts [not yet implemented by Simplyprint.io for Duet]
- GCode terminal [not yet implemented by Simplyprint.io for Duet]
- Receive messages from Printer in Simplyprint.io [not yet implemented by Simplyprint.io for Duet]


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
    pip install meltingplot.duet_simplyprint_connector
    simplyprint autodiscover --password=reprap --ipv4-range=192.168.1.0/24 --ipv6-range=::1/128
    
    # optional - add webcam snapshot uri
    vi ~/.config/SimplyPrint/DuetConnector.json
    
    simplyprint install-as-service

-----------------------------
Content of DuetConnector.json
-----------------------------

The default password for the Duet is `reprap`, even if the web interface does not require a login.

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
            "duet_unique_id": "YOUR_DUET_BOARD_ID",
            "duet_name": "YOUR_DUET_NAME",
            "webcam_uri": "http://URI_OF_WEBCAM_SNAPSHOT_ENDPOINT/snapshot"
        }
    ]


-----------------------------------------------
Usage of Meltingplot Duet Simplyprint Connector
-----------------------------------------------

- Create a configuration with `simplyprint autodiscover`
- *Optional* Edit the configuration file `~/.config/SimplyPrint/DuetConnector.json`
- Start the duet simplyprint connector with `simplyprint start` or `systemctl start simplyprint-connector.service`
- Add the printer via the Simplyprint.io web interface.
