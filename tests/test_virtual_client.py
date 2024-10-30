"""Tests for the VirtualClient class."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

import pytest

from .context import Demands, FileProgressState, VirtualClient, VirtualConfig
from meltingplot.duet_simplyprint_connector.virtual_client import merge_dictionary

@pytest.fixture
def virtual_client():
    """Return a VirtualClient instance."""
    config = VirtualConfig(
        id="virtual",
        token="token",
        unique_id="unique_id",
        duet_uri="http://example.com",
        duet_password="password",
        duet_unique_id="unique_id",
        webcam_uri="http://webcam.example.com"
    )
    client = VirtualClient(config=config)
    return client


@pytest.mark.asyncio
async def test_download_and_upload_file_progress_calculation(virtual_client):
    """Test that the file progress is calculated correctly."""
    event = Demands.FileEvent(
        name="demand",
        demand="file",
    )
    event.url = "http://example.com/file.gcode"
    event.file_name = "file.gcode"
    event.auto_start = True

    mock_duet = AsyncMock()
    virtual_client.duet = mock_duet
    virtual_client.on_start_print = Mock()
    asyncio.run_coroutine_threadsafe = Mock()
    virtual_client.event_loop = asyncio.get_event_loop()
    mock_duet.rr_upload_stream.return_value = {"err": 0}
    mock_duet.rr_fileinfo.return_value = {"err": 0}

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value.__aenter__.return_value.read = AsyncMock(return_value=b"file content")

        task = await virtual_client._download_file_from_sp_and_upload_to_duet(event)
        await task

        assert virtual_client.printer.file_progress.percent == 100.0
        assert virtual_client.printer.file_progress.state == FileProgressState.READY


@pytest.mark.asyncio
async def test_download_and_upload_file_progress_between_90_and_100(virtual_client):
    """Test that the file progress is calculated correctly."""
    event = Demands.FileEvent(
        name="demand",
        demand="file",
    )
    event.url = "http://example.com/file.gcode"
    event.file_name = "file.gcode"
    event.auto_start = True

    mock_duet = AsyncMock()
    virtual_client.duet = mock_duet
    virtual_client.on_start_print = Mock()
    virtual_client.event_loop = asyncio.get_event_loop()
    mock_duet.rr_upload_stream.return_value = {"err": 0}
    mock_duet.rr_fileinfo.side_effect = [{"err": 1}, {"err": 1}, {"err": 0}]
    asyncio.run_coroutine_threadsafe = Mock()

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value.__aenter__.return_value.read = AsyncMock(return_value=b"file content")

        expected_percent = iter([92.5, 98.75, 100.0])
        # Check the values this variable is set to

        def set_percent(change):
            value = next(expected_percent)
            assert value == change["new"]

        virtual_client.printer.file_progress.observe(
            set_percent,
            names=["percent"],
        )

        with patch("time.time", side_effect=[0, 0, 100, 100, 350, 350, 380]):
            task = await virtual_client._download_file_from_sp_and_upload_to_duet(event)
            await task

            assert virtual_client.printer.file_progress.percent == 100.0
            assert virtual_client.printer.file_progress.state == FileProgressState.READY


@pytest.mark.parametrize(
    "source, destination, expected",
    [
        (
            {'a': 1, 'b': {'c': 2}},
            {'b': {'c': 3}},
            {'a': 1, 'b': {'c': 3}}
        ),
        (
            {'a': 1, 'b': {'c': 2, 'd': 4}},
            {'b': {'c': 3, 'e': 5}},
            {'a': 1, 'b': {'c': 3, 'd': 4, 'e': 5}}
        ),
        (
            {'a': 1, 'b': {'c': {'d': 4, 'f': 5}}},
            {'b': {'c': {'d': 5, 'e': 6}}},
            {'a': 1, 'b': {'c': {'d': 5, 'e': 6, 'f': 5}}}
        ),
        (
            {'a': 1, 'b': {'c': 2}},
            {'b': {'d': 3}},
            {'a': 1, 'b': {'c': 2, 'd': 3}}
        ),
        (
            {'a': 1},
            {'b': {'c': 3}},
            {'a': 1, 'b': {'c': 3}}
        ),
        (
            {'a': 1, 'b': [{'c': 2}, {'d': 4}]},
            {'b': [{'c': 3}, None]},
            {'a': 1, 'b': [{'c': 3}, {'d': 4}]}
        ),
        (
            {'result': {'boards': [{'drivers': [{'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 0}, {'status': 0}], 'firmwareDate': '2024-10-07', 'firmwareFileName': 'Duet2CombinedFirmware.bin', 'firmwareName': 'RepRapFirmware for Duet 2 WiFi/Ethernet', 'firmwareVersion': '3.6.0-beta.1+1', 'freeRam': 26112, 'iapFileNameSD': 'Duet2_SDiap32_WiFiEth.bin', 'mcuTemp': {'current': 22.6, 'max': 22.8, 'min': 10.8}, 'name': 'Duet 2 WiFi', 'shortName': '2WiFi', 'uniqueId': '08DJM-9178L-L4MSJ-6J1FL-3S86J-TB2LN', 'vIn': {'current': 24.0, 'max': 24.3, 'min': 0.1}, 'wifiFirmwareFileName': 'DuetWiFiServer.bin'}], 'directories': {'system': '0:/sys/'}, 'fans': [{'actualValue': 0, 'blip': 0.1, 'frequency': 250, 'max': 1.0, 'min': 0.1, 'name': '', 'requestedValue': 0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'sensors': []}}, {'actualValue': 0, 'blip': 0, 'frequency': 30000, 'max': 1.0, 'min': 1.0, 'name': '', 'requestedValue': 1.0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'highTemperature': 45.0, 'lowTemperature': 45.0, 'sensors': [2]}}, {'actualValue': 0, 'blip': 0.25, 'frequency': 30000, 'max': 1.0, 'min': 0.35, 'name': '', 'requestedValue': 1.0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'highTemperature': 30.0, 'lowTemperature': 30.0, 'sensors': [3]}}], 'global': {'filament_extrusion_temp_compensation_factor': 1, 'is_compensated': False, 'compensated_temp': 0, 'initial_temp': 0}, 'heat': {'bedHeaters': [0, -1, -1, -1], 'chamberHeaters': [-1, -1, -1, -1], 'coldExtrudeTemperature': 160.0, 'coldRetractTemperature': 90.0, 'heaters': [{'active': 0, 'avgPwm': 0, 'current': 19.41, 'max': 120.0, 'maxBadReadings': 3, 'maxHeatingFaultTime': 5.0, 'maxTempExcursion': 10.0, 'min': -273.1, 'model': {'coolingExp': 1.4, 'coolingRate': 0.224, 'deadTime': 2.2, 'enabled': True, 'fanCoolingRate': 0, 'heatingRate': 0.432, 'inverted': False, 'maxPwm': 1.0, 'pid': {'d': 1.134, 'i': 0.0777, 'overridden': False, 'p': 0.74671, 'used': True}, 'standardVoltage': 24.4}, 'monitors': [{'action': 0, 'condition': 'tooHigh', 'limit': 120.0, 'sensor': 0}, {'condition': 'disabled', 'sensor': -1}, {'condition': 'disabled', 'sensor': -1}], 'sensor': 0, 'standby': 0, 'state': 'off'}, {'active': 0, 'avgPwm': 0, 'current': 22.24, 'max': 350.0, 'maxBadReadings': 3, 'maxHeatingFaultTime': 25.0, 'maxTempExcursion': 15.0, 'min': -273.1, 'model': {'coolingExp': 1.4, 'coolingRate': 0.205, 'deadTime': 5.8, 'enabled': True, 'fanCoolingRate': 0.114, 'heatingRate': 1.962, 'inverted': False, 'maxPwm': 1.0, 'pid': {'d': 0.25, 'i': 0.0031, 'overridden': False, 'p': 0.06141, 'used': True}, 'standardVoltage': 24.0}, 'monitors': [{'action': 0, 'condition': 'tooHigh', 'limit': 350.0, 'sensor': 2}, {'condition': 'disabled', 'sensor': -1}, {'condition': 'disabled', 'sensor': -1}], 'sensor': 2, 'standby': 0, 'state': 'off'}]}, 'inputs': [{'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'HTTP', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'Marlin', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Telnet', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'File', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'Marlin', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'USB', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Aux', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Trigger', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Queue', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'LCD', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}, None, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': True, 'inverseTimeMode': False, 'lineNumber': 72, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Daemon', 'selectedPlane': 0, 'stackDepth': 1, 'state': 'waiting', 'volumetric': False}, None, {'active': True, 'axesRelative': False, 'compatibility': 'RepRapFirmware', 'distanceUnit': 'mm', 'drivesRelative': True, 'feedRate': 50.0, 'inMacro': False, 'inverseTimeMode': False, 'lineNumber': 0, 'macroRestartable': False, 'motionSystem': 0, 'name': 'Autopause', 'selectedPlane': 0, 'stackDepth': 0, 'state': 'idle', 'volumetric': False}], 'job': {'file': {'customInfo': {}, 'filament': [], 'height': 0, 'layerHeight': 0, 'numLayers': 0, 'size': 0, 'thumbnails': []}, 'filePosition': 0, 'lastDuration': 0, 'lastWarmUpDuration': 0, 'timesLeft': {}}, 'ledStrips': [], 'limits': {}, 'move': {'axes': [{'acceleration': 3000.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['0'], 'homed': False, 'jerk': 480.0, 'letter': 'X', 'machinePosition': 0, 'max': 851.0, 'maxProbed': False, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': False, 'percentCurrent': 100, 'reducedAcceleration': 1000.0, 'speed': 20000.0, 'stepsPerMm': 80.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}, {'acceleration': 3000.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['1'], 'homed': False, 'jerk': 480.0, 'letter': 'Y', 'machinePosition': 0, 'max': 405.0, 'maxProbed': False, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': False, 'percentCurrent': 100, 'reducedAcceleration': 1000.0, 'speed': 20000.0, 'stepsPerMm': 80.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}, {'acceleration': 72.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['2', '3', '4'], 'homed': False, 'jerk': 18.0, 'letter': 'Z', 'machinePosition': 0, 'max': 392.98, 'maxProbed': True, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': True, 'percentCurrent': 100, 'reducedAcceleration': 72.0, 'speed': 1200.0, 'stepsPerMm': 400.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}], 'backlashFactor': 10, 'calibration': {'final': {'deviation': 0, 'mean': 0}, 'initial': {'deviation': 0, 'mean': 0}, 'numFactors': 0}, 'compensation': {'fadeHeight': 15.0, 'probeGrid': {'axes': ['X', 'Y'], 'maxs': [842.4, 379.5], 'mins': [8.6, 25.5], 'radius': -1.0, 'spacings': [49.0, 50.6]}, 'skew': {'compensateXY': True, 'tanXY': 0, 'tanXZ': 0, 'tanYZ': 0}, 'type': 'none'}, 'currentMove': {'acceleration': 0, 'deceleration': 0, 'extrusionRate': 0, 'requestedSpeed': 0, 'topSpeed': 0}, 'extruders': [{'acceleration': 1500.0, 'current': 1500, 'driver': '5', 'factor': 1.0, 'filament': 'PLA matt 0.8mm', 'filamentDiameter': 2.85, 'jerk': 180.0, 'microstepping': {'interpolated': True, 'value': 16}, 'nonlinear': {'a': 0, 'b': 0.011, 'upperLimit': 0.2}, 'percentCurrent': 100, 'position': 0, 'pressureAdvance': 0.035, 'rawPosition': 0, 'speed': 3600.0, 'stepsPerMm': 807.5}], 'idle': {'factor': 0.5, 'timeout': 30.0}, 'kinematics': {'forwardMatrix': [[0.5, 0.5, 0], [0.5, -0.5, 0], [0, 0, 1.0]], 'inverseMatrix': [[1.0, 1.0, 0], [1.0, -1.0, 0], [0, 0, 1.0]], 'name': 'coreXY', 'tiltCorrection': {'correctionFactor': 1.0, 'lastCorrections': [0, 0, 0], 'maxCorrection': 10.0, 'screwPitch': 0.5, 'screwX': [-150.0, 915.0, 915.0], 'screwY': [208.5, 373.5, 43.5]}, 'segmentation': {'minSegLength': 1.0, 'segmentsPerSec': 4.0}}, 'limitAxes': True, 'noMovesBeforeHoming': True, 'printingAcceleration': 800.0, 'queue': [{'gracePeriod': 0.01, 'length': 40}], 'rotation': {'angle': 0, 'centre': [0, 0]}, 'shaping': {'amplitudes': [0.085, 0.289, 0.37, 0.211, 0.045], 'damping': 0.05, 'delays': [0, 0.01252, 0.02503, 0.03755, 0.05006], 'frequency': 40.0, 'type': 'zvddd'}, 'speedFactor': 1.0, 'travelAcceleration': 1250.0, 'virtualEPos': 0, 'workplaceNumber': 0}, 'network': {'corsSite': '', 'hostname': 'meltingplotmbl1', 'interfaces': [{'actualIP': '10.42.0.2', 'firmwareVersion': '2.1.0', 'gateway': '0.0.0.0', 'mac': '00:00:00:00:00:00', 'ssid': 'Meltingplot_A1_539,3', 'state': 'active', 'subnet': '255.255.255.0', 'type': 'wifi'}], 'name': 'Meltingplot.MBL 136 - m83s3j'}, 'sensors': {'analog': [{'beta': 4598.0, 'c': 0.0, 'r25': 100000.0, 'rRef': 2200.0, 'port': '(e2temp,duex.e2temp,exp.thermistor3,exp.35)', 'lastReading': 19.49, 'name': 'bed', 'offsetAdj': 0, 'slopeAdj': 0, 'state': 'ok', 'type': 'thermistor'}, None, {'rRef': 400, 'port': 'spi.cs1', 'lastReading': 21.91, 'name': 'hotend', 'offsetAdj': 0, 'slopeAdj': 0, 'state': 'ok', 'type': 'rtdmax31865'}, {'lastReading': 22.38, 'name': 'mcu-temp', 'offsetAdj': 0, 'slopeAdj': 0, 'state': 'ok', 'type': 'mcutemp'}], 'endstops': [{'highEnd': False, 'triggered': False, 'type': 'inputPin'}, {'highEnd': True, 'triggered': False, 'type': 'inputPin'}, {'highEnd': True, 'triggered': False, 'type': 'motorStallIndividual'}], 'filamentMonitors': [{'configured': {'allMoves': False, 'mmPerRev': 25.3, 'percentMax': 150, 'percentMin': 70, 'sampleDistance': 3.0}, 'position': 0, 'totalExtrusion': 0, 'enableMode': 1, 'status': 'ok', 'type': 'rotatingMagnet'}], 'gpIn': [{'value': 0}, {'value': 0}, {'value': 1}, {'value': 1}], 'probes': [{'calibrationTemperature': 87.5, 'deployedByUser': False, 'disablesHeaters': False, 'diveHeights': [8.0, 8.0], 'lastStopHeight': 0, 'maxProbeCount': 3, 'offsets': [8.6, 25.5, -2.51], 'recoveryTime': 0, 'speeds': [240.0, 240.0], 'temperatureCoefficients': [0.00118, 0], 'threshold': 500, 'tolerance': 0.03, 'travelSpeed': 14400.0, 'triggerHeight': 2.51, 'type': 1, 'value': [0]}]}, 'seqs': {'boards': 459, 'directories': 0, 'fans': 6, 'global': 4, 'heat': 14, 'inputs': 705, 'job': 1, 'ledStrips': 0, 'move': 73, 'network': 49, 'reply': 0, 'sensors': 9, 'spindles': 0, 'state': 5, 'tools': 6, 'volChanges': [1, 0], 'volumes': 383}, 'spindles': [{'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}], 'state': {'atxPower': True, 'atxPowerPort': '!pson', 'currentTool': -1, 'deferredPowerDown': False, 'displayMessage': '', 'gpOut': [{'freq': 500, 'pwm': 0.5}, None, None, {'freq': 500, 'pwm': 1.0}], 'logFile': '0:/sys/eventlog.log', 'logLevel': 'warn', 'machineMode': 'FFF', 'macroRestarted': False, 'msUpTime': 561, 'nextTool': -1, 'powerFailScript': 'M913 X0 Y0 Z10 E10 G91 M83 G1 Z390 E-20 F1500', 'previousTool': -1, 'restorePoints': [{'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}], 'status': 'idle', 'time': '2024-10-14T12:09:01', 'upTime': 575}, 'tools': [{'active': [0], 'axes': [[0], [1], [2]], 'extruders': [0], 'fans': [0], 'feedForward': [0], 'filamentExtruder': 0, 'heaters': [1], 'isRetracted': False, 'mix': [1.0], 'name': '', 'number': 0, 'offsets': [0, 0, 0], 'offsetsProbed': 0, 'retraction': {'extraRestart': 0, 'length': 1.0, 'speed': 27.0, 'unretractSpeed': 14.0, 'zHop': 0.1}, 'spindle': -1, 'spindleRpm': 0, 'standby': [0], 'state': 'off', 'temperatureFeedForward': [0]}], 'volumes': [{'capacity': 31914983424, 'freeSpace': 30538039296, 'mounted': True, 'openFiles': True, 'partitionSize': 31902400512, 'speed': 20000000}, {'mounted': False}]}},
            {'key': '', 'flags': 'fd99', 'result': {'boards': [{'drivers': [{'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 0}, {'status': 0}], 'freeRam': 26112, 'mcuTemp': {'current': 26.0}, 'vIn': {'current': 24.0}}], 'fans': [{'actualValue': 0, 'requestedValue': 0, 'rpm': -1}, {'actualValue': 0, 'requestedValue': 1.0, 'rpm': -1}, {'actualValue': 0, 'requestedValue': 1.0, 'rpm': -1}], 'heat': {'heaters': [{'active': 0, 'avgPwm': 0, 'current': 19.33, 'standby': 0, 'state': 'off'}, {'active': 0, 'avgPwm': 0, 'current': 23.09, 'standby': 0, 'state': 'off'}]}, 'inputs': [{'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, None, {'feedRate': 50.0, 'inMacro': True, 'lineNumber': 72, 'state': 'waiting'}, None, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, None, None], 'job': {'filePosition': 0, 'timesLeft': {}}, 'move': {'axes': [{'machinePosition': 0, 'userPosition': 0}, {'machinePosition': 0, 'userPosition': 0}, {'machinePosition': 0, 'userPosition': 0}], 'currentMove': {'acceleration': 0, 'deceleration': 0, 'extrusionRate': 0, 'requestedSpeed': 0, 'topSpeed': 0}, 'extruders': [{'position': 0, 'rawPosition': 0}], 'virtualEPos': 0}, 'sensors': {'analog': [{'lastReading': 19.33}, None, {'lastReading': 23.09}, {'lastReading': 25.97}], 'endstops': [{'triggered': False}, {'triggered': False}, {'triggered': False}], 'filamentMonitors': [{'position': 0, 'totalExtrusion': 0, 'status': 'ok'}], 'gpIn': [{'value': 0}, {'value': 0}, {'value': 1}, {'value': 1}], 'probes': [{'value': [0]}]}, 'seqs': {'boards': 586, 'directories': 0, 'fans': 6, 'global': 4, 'heat': 14, 'inputs': 845, 'job': 1, 'ledStrips': 0, 'move': 73, 'network': 49, 'reply': 0, 'sensors': 9, 'spindles': 0, 'state': 5, 'tools': 6, 'volChanges': [1, 0], 'volumes': 453}, 'spindles': [{'current': 0, 'state': 'unconfigured'}, {'current': 0, 'state': 'unconfigured'}, {'current': 0, 'state': 'unconfigured'}, {'current': 0, 'state': 'unconfigured'}], 'state': {'currentTool': -1, 'gpOut': [{'pwm': 0.5}, None, None, {'pwm': 1.0}], 'msUpTime': 811, 'status': 'idle', 'time': '2024-10-14T12:22:09', 'upTime': 1363}, 'tools': [{'active': [0], 'isRetracted': False, 'standby': [0], 'state': 'off'}]}},
            {'key': '', 'flags': 'fd99', 'result': {'boards': [{'drivers': [{'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 3284205568}, {'status': 0}, {'status': 0}], 'firmwareDate': '2024-10-07', 'firmwareFileName': 'Duet2CombinedFirmware.bin', 'firmwareName': 'RepRapFirmware for Duet 2 WiFi/Ethernet', 'firmwareVersion': '3.6.0-beta.1+1', 'freeRam': 26112, 'iapFileNameSD': 'Duet2_SDiap32_WiFiEth.bin', 'mcuTemp': {'current': 26.0, 'max': 22.8, 'min': 10.8}, 'name': 'Duet 2 WiFi', 'shortName': '2WiFi', 'uniqueId': '08DJM-9178L-L4MSJ-6J1FL-3S86J-TB2LN', 'vIn': {'current': 24.0, 'max': 24.3, 'min': 0.1}, 'wifiFirmwareFileName': 'DuetWiFiServer.bin'}], 'directories': {'system': '0:/sys/'}, 'fans': [{'actualValue': 0, 'blip': 0.1, 'frequency': 250, 'max': 1.0, 'min': 0.1, 'name': '', 'requestedValue': 0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'sensors': []}}, {'actualValue': 0, 'blip': 0, 'frequency': 30000, 'max': 1.0, 'min': 1.0, 'name': '', 'requestedValue': 1.0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'highTemperature': 45.0, 'lowTemperature': 45.0, 'sensors': [2]}}, {'actualValue': 0, 'blip': 0.25, 'frequency': 30000, 'max': 1.0, 'min': 0.35, 'name': '', 'requestedValue': 1.0, 'rpm': -1, 'tachoPpr': 2.0, 'thermostatic': {'highTemperature': 30.0, 'lowTemperature': 30.0, 'sensors': [3]}}], 'global': {'compensated_temp': 0, 'filament_extrusion_temp_compensation_factor': 1, 'initial_temp': 0, 'is_compensated': False}, 'heat': {'bedHeaters': [0, -1, -1, -1], 'chamberHeaters': [-1, -1, -1, -1], 'coldExtrudeTemperature': 160.0, 'coldRetractTemperature': 90.0, 'heaters': [{'active': 0, 'avgPwm': 0, 'current': 19.33, 'max': 120.0, 'maxBadReadings': 3, 'maxHeatingFaultTime': 5.0, 'maxTempExcursion': 10.0, 'min': -273.1, 'model': {'coolingExp': 1.4, 'coolingRate': 0.224, 'deadTime': 2.2, 'enabled': True, 'fanCoolingRate': 0, 'heatingRate': 0.432, 'inverted': False, 'maxPwm': 1.0, 'pid': {'d': 1.134, 'i': 0.0777, 'overridden': False, 'p': 0.74671, 'used': True}, 'standardVoltage': 24.4}, 'monitors': [{'action': 0, 'condition': 'tooHigh', 'limit': 120.0, 'sensor': 0}, {'condition': 'disabled', 'sensor': -1}, {'condition': 'disabled', 'sensor': -1}], 'sensor': 0, 'standby': 0, 'state': 'off'}, {'active': 0, 'avgPwm': 0, 'current': 23.09, 'max': 350.0, 'maxBadReadings': 3, 'maxHeatingFaultTime': 25.0, 'maxTempExcursion': 15.0, 'min': -273.1, 'model': {'coolingExp': 1.4, 'coolingRate': 0.205, 'deadTime': 5.8, 'enabled': True, 'fanCoolingRate': 0.114, 'heatingRate': 1.962, 'inverted': False, 'maxPwm': 1.0, 'pid': {'d': 0.25, 'i': 0.0031, 'overridden': False, 'p': 0.06141, 'used': True}, 'standardVoltage': 24.0}, 'monitors': [{'action': 0, 'condition': 'tooHigh', 'limit': 350.0, 'sensor': 2}, {'condition': 'disabled', 'sensor': -1}, {'condition': 'disabled', 'sensor': -1}], 'sensor': 2, 'standby': 0, 'state': 'off'}]}, 'inputs': [{'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, None, {'feedRate': 50.0, 'inMacro': True, 'lineNumber': 72, 'state': 'waiting'}, None, {'feedRate': 50.0, 'inMacro': False, 'lineNumber': 0, 'state': 'idle'}, None, None], 'job': {'file': {'customInfo': {}, 'filament': [], 'height': 0, 'layerHeight': 0, 'numLayers': 0, 'size': 0, 'thumbnails': []}, 'filePosition': 0, 'lastDuration': 0, 'lastWarmUpDuration': 0, 'timesLeft': {}}, 'ledStrips': [], 'limits': {}, 'move': {'axes': [{'acceleration': 3000.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['0'], 'homed': False, 'jerk': 480.0, 'letter': 'X', 'machinePosition': 0, 'max': 851.0, 'maxProbed': False, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': False, 'percentCurrent': 100, 'reducedAcceleration': 1000.0, 'speed': 20000.0, 'stepsPerMm': 80.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}, {'acceleration': 3000.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['1'], 'homed': False, 'jerk': 480.0, 'letter': 'Y', 'machinePosition': 0, 'max': 405.0, 'maxProbed': False, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': False, 'percentCurrent': 100, 'reducedAcceleration': 1000.0, 'speed': 20000.0, 'stepsPerMm': 80.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}, {'acceleration': 72.0, 'babystep': 0, 'backlash': 0, 'current': 2000, 'drivers': ['2', '3', '4'], 'homed': False, 'jerk': 18.0, 'letter': 'Z', 'machinePosition': 0, 'max': 392.98, 'maxProbed': True, 'microstepping': {'interpolated': True, 'value': 16}, 'min': 0, 'minProbed': True, 'percentCurrent': 100, 'reducedAcceleration': 72.0, 'speed': 1200.0, 'stepsPerMm': 400.0, 'userPosition': 0, 'visible': True, 'workplaceOffsets': [0, 0, 0, 0, 0, 0, 0, 0, 0]}], 'backlashFactor': 10, 'calibration': {'final': {'deviation': 0, 'mean': 0}, 'initial': {'deviation': 0, 'mean': 0}, 'numFactors': 0}, 'compensation': {'fadeHeight': 15.0, 'probeGrid': {'axes': ['X', 'Y'], 'maxs': [842.4, 379.5], 'mins': [8.6, 25.5], 'radius': -1.0, 'spacings': [49.0, 50.6]}, 'skew': {'compensateXY': True, 'tanXY': 0, 'tanXZ': 0, 'tanYZ': 0}, 'type': 'none'}, 'currentMove': {'acceleration': 0, 'deceleration': 0, 'extrusionRate': 0, 'requestedSpeed': 0, 'topSpeed': 0}, 'extruders': [{'acceleration': 1500.0, 'current': 1500, 'driver': '5', 'factor': 1.0, 'filament': 'PLA matt 0.8mm', 'filamentDiameter': 2.85, 'jerk': 180.0, 'microstepping': {'interpolated': True, 'value': 16}, 'nonlinear': {'a': 0, 'b': 0.011, 'upperLimit': 0.2}, 'percentCurrent': 100, 'position': 0, 'pressureAdvance': 0.035, 'rawPosition': 0, 'speed': 3600.0, 'stepsPerMm': 807.5}], 'idle': {'factor': 0.5, 'timeout': 30.0}, 'kinematics': {'forwardMatrix': [[0.5, 0.5, 0], [0.5, -0.5, 0], [0, 0, 1.0]], 'inverseMatrix': [[1.0, 1.0, 0], [1.0, -1.0, 0], [0, 0, 1.0]], 'name': 'coreXY', 'segmentation': {'minSegLength': 1.0, 'segmentsPerSec': 4.0}, 'tiltCorrection': {'correctionFactor': 1.0, 'lastCorrections': [0, 0, 0], 'maxCorrection': 10.0, 'screwPitch': 0.5, 'screwX': [-150.0, 915.0, 915.0], 'screwY': [208.5, 373.5, 43.5]}}, 'limitAxes': True, 'noMovesBeforeHoming': True, 'printingAcceleration': 800.0, 'queue': [{'gracePeriod': 0.01, 'length': 40}], 'rotation': {'angle': 0, 'centre': [0, 0]}, 'shaping': {'amplitudes': [0.085, 0.289, 0.37, 0.211, 0.045], 'damping': 0.05, 'delays': [0, 0.01252, 0.02503, 0.03755, 0.05006], 'frequency': 40.0, 'type': 'zvddd'}, 'speedFactor': 1.0, 'travelAcceleration': 1250.0, 'virtualEPos': 0, 'workplaceNumber': 0}, 'network': {'corsSite': '', 'hostname': 'meltingplotmbl1', 'interfaces': [{'actualIP': '10.42.0.2', 'firmwareVersion': '2.1.0', 'gateway': '0.0.0.0', 'mac': '00:00:00:00:00:00', 'ssid': 'Meltingplot_A1_539,3', 'state': 'active', 'subnet': '255.255.255.0', 'type': 'wifi'}], 'name': 'Meltingplot.MBL 136 - m83s3j'}, 'sensors': {'analog': [{'beta': 4598.0, 'c': 0.0, 'lastReading': 19.33, 'name': 'bed', 'offsetAdj': 0, 'port': '(e2temp,duex.e2temp,exp.thermistor3,exp.35)', 'r25': 100000.0, 'rRef': 2200.0, 'slopeAdj': 0, 'state': 'ok', 'type': 'thermistor'}, None, {'lastReading': 23.09, 'name': 'hotend', 'offsetAdj': 0, 'port': 'spi.cs1', 'rRef': 400, 'slopeAdj': 0, 'state': 'ok', 'type': 'rtdmax31865'}, {'lastReading': 25.97, 'name': 'mcu-temp', 'offsetAdj': 0, 'slopeAdj': 0, 'state': 'ok', 'type': 'mcutemp'}], 'endstops': [{'highEnd': False, 'triggered': False, 'type': 'inputPin'}, {'highEnd': True, 'triggered': False, 'type': 'inputPin'}, {'highEnd': True, 'triggered': False, 'type': 'motorStallIndividual'}], 'filamentMonitors': [{'configured': {'allMoves': False, 'mmPerRev': 25.3, 'percentMax': 150, 'percentMin': 70, 'sampleDistance': 3.0}, 'enableMode': 1, 'position': 0, 'status': 'ok', 'totalExtrusion': 0, 'type': 'rotatingMagnet'}], 'gpIn': [{'value': 0}, {'value': 0}, {'value': 1}, {'value': 1}], 'probes': [{'calibrationTemperature': 87.5, 'deployedByUser': False, 'disablesHeaters': False, 'diveHeights': [8.0, 8.0], 'lastStopHeight': 0, 'maxProbeCount': 3, 'offsets': [8.6, 25.5, -2.51], 'recoveryTime': 0, 'speeds': [240.0, 240.0], 'temperatureCoefficients': [0.00118, 0], 'threshold': 500, 'tolerance': 0.03, 'travelSpeed': 14400.0, 'triggerHeight': 2.51, 'type': 1, 'value': [0]}]}, 'seqs': {'boards': 586, 'directories': 0, 'fans': 6, 'global': 4, 'heat': 14, 'inputs': 845, 'job': 1, 'ledStrips': 0, 'move': 73, 'network': 49, 'reply': 0, 'sensors': 9, 'spindles': 0, 'state': 5, 'tools': 6, 'volChanges': [1, 0], 'volumes': 453}, 'spindles': [{'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}, {'active': 0, 'canReverse': False, 'current': 0, 'state': 'unconfigured'}], 'state': {'atxPower': True, 'atxPowerPort': '!pson', 'currentTool': -1, 'deferredPowerDown': False, 'displayMessage': '', 'gpOut': [{'freq': 500, 'pwm': 0.5}, None, None, {'freq': 500, 'pwm': 1.0}], 'logFile': '0:/sys/eventlog.log', 'logLevel': 'warn', 'machineMode': 'FFF', 'macroRestarted': False, 'msUpTime': 811, 'nextTool': -1, 'powerFailScript': 'M913 X0 Y0 Z10 E10 G91 M83 G1 Z390 E-20 F1500', 'previousTool': -1, 'restorePoints': [{'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}, {'coords': [0, 0, 0], 'extruderPos': 0, 'fanPwm': 0, 'feedRate': 50.0, 'ioBits': 0, 'toolNumber': -1}], 'status': 'idle', 'time': '2024-10-14T12:22:09', 'upTime': 1363}, 'tools': [{'active': [0], 'axes': [[0], [1], [2]], 'extruders': [0], 'fans': [0], 'feedForward': [0], 'filamentExtruder': 0, 'heaters': [1], 'isRetracted': False, 'mix': [1.0], 'name': '', 'number': 0, 'offsets': [0, 0, 0], 'offsetsProbed': 0, 'retraction': {'extraRestart': 0, 'length': 1.0, 'speed': 27.0, 'unretractSpeed': 14.0, 'zHop': 0.1}, 'spindle': -1, 'spindleRpm': 0, 'standby': [0], 'state': 'off', 'temperatureFeedForward': [0]}], 'volumes': [{'capacity': 31914983424, 'freeSpace': 30538039296, 'mounted': True, 'openFiles': True, 'partitionSize': 31902400512, 'speed': 20000000}, {'mounted': False}]}}
        )
    ]
)
def test_merge(source, destination, expected):
    """Test the merge function."""
    result = merge_dictionary(source, destination)
    assert result == expected

@pytest.mark.asyncio
async def test_fetch_rr_model_success(virtual_client):
    """Test _fetch_rr_model with a successful response."""
    key = "job"
    expected_response = {"result": {"file": {"filename": "test.gcode"}}}

    virtual_client.duet.rr_model = AsyncMock(return_value=expected_response)

    response = await virtual_client._fetch_rr_model(key=key)

    assert response == expected_response
    virtual_client.duet.rr_model.assert_called_once_with(key=key)


@pytest.mark.asyncio
async def test_fetch_rr_model_client_connection_error(virtual_client):
    """Test _fetch_rr_model with a ClientConnectionError."""
    key = "job"
    return_on_timeout = {"timeout": True}

    virtual_client.duet.rr_model = AsyncMock(side_effect=aiohttp.ClientConnectionError)

    response = await virtual_client._fetch_rr_model(key=key, return_on_timeout=return_on_timeout)

    assert response == return_on_timeout
    virtual_client.duet.rr_model.assert_called_once_with(key=key)


@pytest.mark.asyncio
async def test_fetch_rr_model_timeout_error(virtual_client):
    """Test _fetch_rr_model with a TimeoutError."""
    key = "job"
    return_on_timeout = {"timeout": True}

    virtual_client.duet.rr_model = AsyncMock(side_effect=TimeoutError)

    response = await virtual_client._fetch_rr_model(key=key, return_on_timeout=return_on_timeout)

    assert response == return_on_timeout
    virtual_client.duet.rr_model.assert_called_once_with(key=key)


@pytest.mark.asyncio
async def test_fetch_rr_model_other_exception(virtual_client):
    """Test _fetch_rr_model with a generic exception."""
    key = "job"
    return_on_exception = {"exception": True}

    virtual_client.duet.rr_model = AsyncMock(side_effect=Exception("Test Exception"))

    response = await virtual_client._fetch_rr_model(key=key, return_on_exception=return_on_exception)

    assert response == return_on_exception
    virtual_client.duet.rr_model.assert_called_once_with(key=key)