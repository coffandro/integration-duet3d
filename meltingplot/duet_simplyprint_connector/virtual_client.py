"""A virtual client for the SimplyPrint.io Service."""

import asyncio
import base64
import csv
import io
import json
import pathlib
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Optional, Union

import aiohttp

import imageio.v3 as iio

import psutil

from simplyprint_ws_client.const import VERSION as SP_VERSION
from simplyprint_ws_client.core.client import ClientConfigChangedEvent, DefaultClient
from simplyprint_ws_client.core.config import PrinterConfig
from simplyprint_ws_client.core.state import FilamentSensorEnum, FileProgressStateEnum, PrinterStatus
from simplyprint_ws_client.core.ws_protocol.messages import (
    FileDemandData,
    GcodeDemandData,
    MeshDataMsg,
    PrinterSettingsMsg,
    StreamMsg,
    WebcamSnapshotDemandData,
)
from simplyprint_ws_client.shared.files.file_download import FileDownload
from simplyprint_ws_client.shared.hardware.physical_machine import PhysicalMachine

from yarl import URL

from . import __version__
from .duet.api import RepRapFirmware
from .duet.model import DuetPrinter
from .gcode import GCodeBlock
from .network import get_local_ip_and_mac

duet_state_simplyprint_status_mapping = {
    'disconnected': PrinterStatus.OFFLINE,
    'starting': PrinterStatus.NOT_READY,
    'updating': PrinterStatus.NOT_READY,
    'off': PrinterStatus.OFFLINE,
    'halted': PrinterStatus.ERROR,
    'pausing': PrinterStatus.PAUSING,
    'paused': PrinterStatus.PAUSED,
    'resuming': PrinterStatus.RESUMING,
    'cancelling': PrinterStatus.CANCELLING,
    'processing': PrinterStatus.PRINTING,
    'simulating': PrinterStatus.OPERATIONAL,
    'busy': PrinterStatus.OPERATIONAL,
    'changingTool': PrinterStatus.OPERATIONAL,
    'idle': PrinterStatus.OPERATIONAL,
}

duet_state_simplyprint_status_while_printing_mapping = {
    'disconnected': PrinterStatus.OFFLINE,
    'starting': PrinterStatus.NOT_READY,
    'updating': PrinterStatus.NOT_READY,
    'off': PrinterStatus.OFFLINE,
    'halted': PrinterStatus.ERROR,
    'pausing': PrinterStatus.PAUSING,
    'paused': PrinterStatus.PAUSED,
    'resuming': PrinterStatus.RESUMING,
    'cancelling': PrinterStatus.CANCELLING,
    'processing': PrinterStatus.PRINTING,
    'simulating': PrinterStatus.NOT_READY,
    'busy': PrinterStatus.PRINTING,
    'changingTool': PrinterStatus.PRINTING,
    'idle': PrinterStatus.OPERATIONAL,
}


def async_task(func):
    """Run a function as a task."""

    async def wrapper(*args, **kwargs):
        task = args[0].event_loop.create_task(func(*args, **kwargs))
        args[0]._background_task.add(task)
        task.add_done_callback(args[0]._background_task.discard)
        return task

    return wrapper


def async_supress(func):
    """Suppress exceptions in an async function."""

    async def wrapper(*args, **kwargs):
        try:
            await func(*args, **kwargs)
        except asyncio.CancelledError as e:
            await args[0].duet.close()
            raise e
        except Exception as e:
            args[0].logger.exception(
                "An exception occurred while running an async function",
                exc_info=e,
            )

    return wrapper


@dataclass
class WebcamSnapshotRequest():
    """Webcam snapshot request."""

    snapshot_id: str = None
    endpoint: Union[str, URL, None] = None


@dataclass
class VirtualConfig(PrinterConfig):
    """Configuration for the VirtualClient."""

    duet_name: Optional[str] = None
    duet_uri: Optional[str] = None
    duet_password: Optional[str] = None
    duet_unique_id: Optional[str] = None
    webcam_uri: Optional[str] = None


class VirtualClient(DefaultClient[VirtualConfig]):
    """A Websocket client for the SimplyPrint.io Service."""

    duet_api: RepRapFirmware
    duet: DuetPrinter

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the client."""
        super().__init__(*args, **kwargs)

    async def init(self) -> None:
        """Initialize the client."""
        self.logger.info('Initializing the client')

        self.duet_api = RepRapFirmware(
            address=self.config.duet_uri,
            password=self.config.duet_password,
            logger=self.logger.getChild('duet_api'),
        )

        self.duet = DuetPrinter(
            logger=self.logger.getChild('duet'),
            api=self.duet_api,
        )

        self.duet.events.on('connect', self._duet_on_connect)
        self.duet.events.on('objectmodel', self.duet_on_objectmodel)

        self._printer_timeout = time.time() + 60 * 5  # 5 minutes

        self._webcam_timeout = 0
        self._webcam_distribution_task_handle = None
        self._requested_webcam_snapshots = asyncio.Queue()
        self._webcam_frame = asyncio.Queue(maxsize=3)

        self._background_task = set()
        self._is_stopped = False

        self.printer.info.core_count = psutil.cpu_count(logical=False)
        self.printer.info.total_memory = psutil.virtual_memory().total
        self.printer.info.hostname = socket.getfqdn()
        self.printer.info.os = "Meltingplot Duet Connector v{!s}".format(__version__)
        self.printer.info.sp_version = SP_VERSION
        self.printer.info.python_version = platform.python_version()
        if self.config.in_setup:
            self.printer.info.machine = self.config.duet_name or self.config.duet_uri
        else:
            self.printer.info.machine = PhysicalMachine.machine()

        request = WebcamSnapshotRequest()
        await self._requested_webcam_snapshots.put(request)

    async def _duet_on_connect(self) -> None:
        """Connect to the Duet board."""
        if self.config.in_setup:
            await self.duet.gcode(
                f'M291 P"Code: {self.config.short_id}" R"Simplyprint.io Setup" S2',
            )
        else:
            await self._check_and_set_cookie()

        board = self.duet.om['boards'][0]
        network = self.duet.om['network']

        if self.config.duet_unique_id is None:
            # Set the unique ID if it is not set
            # and emit an event to notify the client
            # that the configuration has changed
            # TODO: Update the webcam URI
            self.config.duet_unique_id = board['uniqueId']
            await self.event_bus.emit(ClientConfigChangedEvent)
        else:
            if self.config.duet_unique_id != board['uniqueId']:
                self.logger.error(
                    'Unique ID mismatch: {0} != {1}'.format(self.config.duet_unique_id, board['uniqueId']),
                )
                self.printer.status = PrinterStatus.OFFLINE
                # TODO: Implement a search mechanism based on the unique ID
                raise ValueError('Unique ID mismatch')

        # the format of the machine_name should be [MANUFACTURER] [PRINTER MODEL]
        name_search = re.search(
            r'(meltingplot)([-\. ])(MBL[ -]?[0-9]{3})([ -]{0,3})(\w{6})?[ ]?(\w+)?',
            network['name'],
            re.I,
        )
        try:
            printer_name = name_search.group(3).replace('-', ' ').strip()
            self.printer.firmware.machine_name = f"Meltingplot {printer_name}"
        except (AttributeError, IndexError):
            self.printer.firmware.machine_name = network['name']

        self.printer.firmware.name = board['firmwareName']
        self.printer.firmware.version = board['firmwareVersion']
        self.printer.set_api_info("meltingplot.duet-simplyprint-connector", __version__)
        self.printer.set_ui_info("meltingplot.duet-simplyprint-connector", __version__)

    async def duet_on_objectmodel(self, old_om) -> None:
        """Handle Objectmodel changes."""
        self.logger.debug('Objectmodel changed')
        await self._mesh_compensation_status(old_om['move']['compensation'])
        try:
            await self._update_temperatures()
        except KeyError:
            self.printer.bed_temperature.actual = 0.0
            self.printer.tool_temperatures[0].actual = 0.0

        old_printer_state = self.printer.status
        await self._map_duet_state_to_printer_status()

        if self.printer.status == PrinterStatus.CANCELLING and old_printer_state == PrinterStatus.PRINTING:
            self.printer.job_info.cancelled = True
        elif self.printer.status == PrinterStatus.OPERATIONAL:  # The machine is on but has nothing to do
            if self.printer.job_info.started or old_printer_state == PrinterStatus.PRINTING:
                # setting 'finished' will clear 'started'
                self.printer.job_info.finished = True
                self.printer.job_info.progress = 100.0

        if await self._is_printing():
            await self._update_job_info()

        await self._update_filament_sensor()

    @async_task
    async def _duet_printer_task(self):
        """Duet Printer task."""
        while not self._is_stopped:
            try:
                if self._printer_timeout < time.time():
                    self.printer.status = PrinterStatus.OFFLINE
                    await self.duet.close()
                try:
                    if not self.duet.connected():
                        await self.duet.connect()
                    await self.duet.tick()
                except (
                    TypeError,
                    KeyError,
                    aiohttp.ClientConnectionError,
                    aiohttp.ClientResponseError,
                    asyncio.TimeoutError,
                ):
                    self.logger.debug('Failed to connect to Duet')
                    await self.duet.close()
                    await asyncio.sleep(30)
                    continue
                self._printer_timeout = time.time() + 60 * 5
                await asyncio.sleep(0.5)
            except asyncio.CancelledError as e:
                await self.duet.close()
                raise e
            except Exception:
                self.logger.exception(
                    "An exception occurred while ticking duet printer",
                )
                await asyncio.sleep(10)
                continue

    async def on_connected(self, _) -> None:
        """Connect to Simplyprint.io."""
        self.logger.info('Connected to Simplyprint.io')

        self.use_running_loop()
        self._is_stopped = False

        await self._duet_printer_task()
        await self._connector_status_task()

    async def on_remove_connection(self, _) -> None:
        """Remove the connection."""
        self.logger.info('Disconnected from Simplyprint.io')
        self._is_stopped = True
        for task in self._background_task:
            task.cancel()

    async def on_printer_settings(
        self,
        event: PrinterSettingsMsg,
    ) -> None:
        """Update the printer settings."""
        self.logger.debug("Printer settings: %s", event.data)

    @async_task
    async def deferred_gcode(self, event: GcodeDemandData) -> None:
        """Defer the GCode event."""
        self.logger.debug("Received Gcode: {!r}".format(event.list))

        gcode = GCodeBlock().parse(event.list)
        self.logger.debug("Parsed Gcode: {!r}".format(gcode))

        allowed_commands = [
            'M17',
            'M18',
            'M104',
            'M106',
            'M107',
            'M109',
            'M112',
            'M140',
            'M190',
            'M220',
            'M221',
            'G1',
            'G28',
            'G29',
            'G90',
            'G91',
        ]

        response = []

        for item in gcode.code:
            if item.code in allowed_commands and not self.config.in_setup:
                response.append(await self.duet.gcode(item.compress()))
            elif item.code == 'M300' and self.config.in_setup:
                response.append(
                    await self.duet.gcode(
                        f'M291 P"Simplyprint.io Code: {self.config.short_id}" R"Simplyprint Identification" S2',
                    ),
                )
            elif item.code == 'M997':
                # perform self upgrade
                self.logger.info('Performing self upgrade')
                try:
                    subprocess.check_call(
                        [
                            sys.executable,
                            '-m',
                            'pip',
                            'install',
                            '--upgrade',
                            'meltingplot.duet_simplyprint_connector',
                        ],
                    )
                except subprocess.CalledProcessError as e:
                    self.logger.error('Error upgrading: {0}'.format(e))
                self.logger.info("Restarting API")
                # Since the API runs as a systemd service, we can restart it by terminating the process.
                raise KeyboardInterrupt()
            else:
                response.append('{!s} G-Code blocked'.format(item.code))

        # List of GCodes received from SP
        # M104 S1 Tool heater on
        # M140 S1 Bed heater on
        # M106 Fan on
        # M107 Fan off
        # M221 S1 control flow rate
        # M220 S1 control speed factor
        # G91
        # G1 E10
        # G90
        # G1 X10
        # G1 Y10
        # G1 Z10
        # G28 Z
        # G28 XY
        # G29
        # M18
        # M17
        # M190
        # M109
        # M155 # not supported by reprapfirmware

    async def on_gcode(self, event: GcodeDemandData) -> None:
        """
        Receive GCode from SP and send GCode to duet.

        The GCode is checked for allowed commands and then sent to the Duet.
        """
        await self.deferred_gcode(event)

    def _upload_file_progress(self, progress: float) -> None:
        """Update the file upload progress."""
        # contrains the progress from 50 - 90 %
        self.printer.file_progress.percent = min(round(50 + (max(0, min(50, progress / 2))), 0), 90.0)

    async def _auto_start_file(self, event) -> None:
        """Auto start the file after it has been uploaded."""
        self.printer.job_info.filename = event.file_name
        timeout = time.time() + 400  # seconds

        while timeout > time.time():
            try:
                response = await self.duet.api.rr_fileinfo(
                    name="0:/gcodes/{!s}".format(event.file_name),
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            except (
                aiohttp.ClientConnectionError,
                TimeoutError,
                asyncio.exceptions.TimeoutError,
                asyncio.TimeoutError,
            ):
                response = {'err': 1}  # timeout

            if response['err'] == 0:
                break

            timeleft = 10 - ((timeout - time.time()) * 0.025)
            self.printer.file_progress.percent = min(99.9, (90.0 + timeleft))

            await asyncio.sleep(1)
        else:
            raise TimeoutError('Timeout while waiting for file to be ready')

        asyncio.run_coroutine_threadsafe(
            self.on_start_print(event),
            self.event_loop,
        )

    @async_task
    async def _fileprogress_task(self) -> None:
        """
        Periodically send file upload progress updates.

        This task ensures that file upload progress is sent every 5 seconds to prevent
        timeouts on clients with low bandwidth. The progress step between 0.5% can exceed
        the default timeout of 30 seconds, so frequent updates are necessary.
        """
        while not self._is_stopped and self.printer.file_progress.state == FileProgressStateEnum.DOWNLOADING:
            self.printer.file_progress.model_set_changed("state", "percent")
            await asyncio.sleep(5)

    @async_task
    @async_supress
    async def _download_file_from_sp_and_upload_to_duet(
        self,
        event: FileDemandData,
    ) -> None:
        """Download a file from Simplyprint.io and upload it to the printer."""
        downloader = FileDownload(self)

        self.printer.file_progress.state = FileProgressStateEnum.DOWNLOADING
        self.printer.file_progress.percent = 0.0

        # Initiate the file progress task to send updates every 10 seconds.
        # This helps prevent timeouts on clients with low bandwidth or slow connections,
        # as SimplyPrint.io has a timeout of 30 seconds.
        await self._fileprogress_task()

        with tempfile.NamedTemporaryFile(suffix='.gcode') as f:
            async for chunk in downloader.download(
                url=event.url,
                clamp_progress=(lambda x: float(max(0.0, min(50.0, x / 2.0)))),
            ):
                f.write(chunk)

            f.seek(0)
            prefix = '0:/gcodes/'
            retries = 3

            while retries > 0:
                try:
                    # Ensure progress updates are sent during the upload process,
                    # as uploading large files (e.g., 1 GB) can take significant time on a Duet2 Wifi.
                    response = await self.duet.api.rr_upload_stream(
                        filepath='{!s}{!s}'.format(prefix, event.file_name),
                        file=f,
                        progress=self._upload_file_progress,
                    )
                    if response['err'] != 0:
                        self.printer.file_progress.state = FileProgressStateEnum.ERROR
                        return
                    break
                except aiohttp.ClientResponseError as e:
                    if e.status == 401 or e.status == 500:
                        await self.duet.api.reconnect()
                    else:
                        # Ensure the exception is not supressed
                        raise e
                finally:
                    retries -= 1

        if event.auto_start:
            await self._auto_start_file(event)

        self.printer.file_progress.percent = 100.0
        self.printer.file_progress.state = FileProgressStateEnum.READY

    async def on_file(self, event: FileDemandData) -> None:
        """Download a file from Simplyprint.io to the printer."""
        await self._download_file_from_sp_and_upload_to_duet(event=event)

    async def on_start_print(self, _) -> None:
        """Start the print job."""
        await self.duet.gcode(
            'M23 "0:/gcodes/{!s}"'.format(self.printer.job_info.filename),
        )
        await self.duet.gcode('M24')

    async def on_pause(self, _) -> None:
        """Pause the print job."""
        await self.duet.gcode('M25')

    async def on_resume(self, _) -> None:
        """Resume the print job."""
        await self.duet.gcode('M24')

    async def on_cancel(self, _) -> None:
        """Cancel the print job."""
        await self.duet.gcode('M25')
        await self.duet.gcode('M0')

    async def _update_temperatures(self) -> None:
        """Update the printer temperatures."""
        heaters = self.duet.om['heat']['heaters']
        # TODO make it aware of more than one bed heater
        bed_heater_index = self.duet.om['heat']['bedHeaters'][0]

        self.printer.bed_temperature.actual = heaters[bed_heater_index]['current']
        if heaters[0]['state'] != 'off':
            self.printer.bed_temperature.target = heaters[bed_heater_index]['active']
        else:
            self.printer.bed_temperature.target = 0.0

        for tool_idx, tool_temperature in enumerate(self.printer.tool_temperatures):
            heater_idx = self.duet.om['tools'][tool_idx]['heaters'][0]
            tool_temperature.actual = heaters[heater_idx]['current']

            if heaters[1]['state'] != 'off':
                tool_temperature.target = heaters[heater_idx]['active']
            else:
                tool_temperature.target = 0.0

        self.printer.ambient_temperature.ambient = 20

    async def _check_and_set_cookie(self) -> None:
        """Check if the cookie is set and set it if it is not."""
        self.logger.debug('Checking if cookie is set')
        try:
            async for chunk in self.duet.api.rr_download(filepath='0:/sys/simplyprint-connector.json'):
                break
            await self.duet.api.rr_delete(filepath='0:/sys/simplyprint-connector.json')
        except aiohttp.client_exceptions.ClientResponseError:
            self.logger.debug('Cookie not set, setting cookie')

        await self.duet.api.rr_upload_stream(
            filepath='0:/sys/simplyprint-connector.json',
            file=io.BytesIO(
                json.dumps(
                    {
                        'hostname': self.printer.info.hostname,
                        'ip': self.printer.info.local_ip,
                        'mac': self.printer.info.mac,
                    },
                ).encode(),
            ),
        )

    @async_task
    async def _mesh_compensation_status(self, old_compensation) -> None:
        """Task to check for mesh compensation changes and send mesh data to SimplyPrint."""
        compensation = self.duet.om['move']['compensation']

        if (
            compensation is not None and 'file' in compensation and compensation['file'] is not None and (
                old_compensation is None or 'file' not in old_compensation
                or old_compensation['file'] != compensation['file']
            )
        ):
            try:
                await self._send_mesh_data()
            except Exception as e:
                self.logger.exception(
                    "An exception occurred while sending mesh data",
                    exc_info=e,
                )

    async def _update_cpu_and_memory_info(self) -> None:
        self.printer.cpu_info.usage = psutil.cpu_percent(interval=1)
        try:
            self.printer.cpu_info.temp = psutil.sensors_temperatures()['coretemp'][0].current
        except KeyError:
            self.printer.cpu_info.temp = 0.0
        self.printer.cpu_info.memory = psutil.virtual_memory().percent

    @async_task
    async def _connector_status_task(self) -> None:
        """Task to gather connector infos and send data to SimplyPrint."""
        while not self._is_stopped:
            await self._update_cpu_and_memory_info()

            netinfo = get_local_ip_and_mac()
            self.printer.info.local_ip = netinfo.ip
            self.printer.info.mac = netinfo.mac

            await asyncio.sleep(120)

    async def _map_duet_state_to_printer_status(self) -> None:
        try:
            printer_state = self.duet.om['state']['status']
        except (KeyError, TypeError):
            printer_state = 'disconnected'

        # SP is sensitive to printer status while printing
        # so we need to differentiate between printer status while printing and not printing
        if await self._is_printing():
            self.printer.status = duet_state_simplyprint_status_while_printing_mapping[printer_state]
        else:
            self.printer.status = duet_state_simplyprint_status_mapping[printer_state]

    async def _update_filament_sensor(self) -> None:
        filament_monitors = self.duet.om['sensors']['filamentMonitors']

        for monitor in filament_monitors:
            if monitor['enableMode'] > 0:
                self.printer.settings.has_filament_settings = True
                if monitor['status'] == 'ok':
                    self.printer.filament_sensor.state = FilamentSensorEnum.LOADED
                else:
                    self.printer.filament_sensor.state = FilamentSensorEnum.RUNOUT
                    break  # only one sensor is needed

                try:
                    if monitor['calibrated'] is None or self.printer.status != PrinterStatus.PAUSED:
                        continue
                    if monitor['calibrated']['percentMin'] < monitor['configured']['percentMin']:
                        self.printer.filament_sensor.state = FilamentSensorEnum.RUNOUT
                        break  # only one sensor is needed
                    if monitor['calibrated']['percentMax'] < monitor['configured']['percentMax']:
                        self.printer.filament_sensor.state = FilamentSensorEnum.RUNOUT
                        break  # only one sensor is needed
                except KeyError:
                    pass

    async def _is_printing(self) -> bool:
        printing = (
            self.printer.status == PrinterStatus.PRINTING or self.printer.status == PrinterStatus.PAUSED
            or self.printer.status == PrinterStatus.PAUSING or self.printer.status == PrinterStatus.RESUMING
        )

        try:
            job_status = self.duet.om['job']['file']
        except (KeyError, TypeError):
            return False

        printing = printing or ('filename' in job_status and job_status['filename'] is not None)

        return printing

    async def _update_times_left(self, times_left: dict) -> None:
        if 'filament' in times_left:
            self.printer.job_info.time = times_left['filament']
        elif 'slicer' in times_left:
            self.printer.job_info.time = times_left['slicer']
        elif 'file' in times_left:
            self.printer.job_info.time = times_left['file']
        else:
            self.printer.job_info.time = 0

    async def _update_job_info(self) -> None:
        job_status = self.duet.om['job']

        try:
            # TODO: Find another way to calculate the progress
            total_filament_required = sum(
                job_status['file']['filament'],
            )
            current_filament = float(job_status['rawExtrusion'])
            self.printer.job_info.progress = min(
                current_filament * 100.0 / total_filament_required,
                100.0,
            )
            self.printer.job_info.filament = round(current_filament, None)
        except (TypeError, KeyError, ZeroDivisionError):
            self.printer.job_info.progress = 0.0

        try:
            await self._update_times_left(times_left=job_status['timesLeft'])
        except (TypeError, KeyError):
            self.printer.job_info.time = 0

        try:
            filepath = job_status['file']['fileName']
            self.printer.job_info.filename = pathlib.PurePath(
                filepath,
            ).name
            if ('duration' in job_status and job_status['duration'] is not None and job_status['duration'] < 10):
                # only set the printjob as startet if the duration is less than 10 seconds
                self.printer.job_info.started = True
        except (TypeError, KeyError):
            # SP is maybe keeping track of print jobs via the file name
            # self.printer.job_info.filename = None
            pass

        self.printer.job_info.layer = job_status['layer'] if 'layer' in job_status else 0

    async def tick(self, _) -> None:
        """Update the client state."""
        try:
            await self.send_ping()
        except Exception as e:
            self.logger.exception(
                "An exception occurred while ticking the client state",
                exc_info=e,
            )

    async def halt(self) -> None:
        """Halt the client."""
        self.logger.debug('halting the client')
        self._is_stopped = True
        for task in self._background_task:
            task.cancel()
        await self.duet.close()

    async def teardown(self) -> None:
        """Teardown the client."""
        pass

    async def on_webcam_test(self) -> None:
        """Test the webcam."""
        self.printer.webcam_info.connected = (True if self.config.webcam_uri is not None else False)

    async def _send_webcam_snapshot(self, image: bytes) -> None:
        jpg_encoded = image
        base64_encoded = base64.b64encode(jpg_encoded).decode()
        # TODO: remove when fixed in simplyprint-ws-client
        while self.printer.intervals.use('webcam') is False:
            await self.printer.intervals.wait_for('webcam')

        await self.send(
            StreamMsg(base64jpg=base64_encoded),
        )

    async def _send_webcam_snapshot_to_endpoint(self, image: bytes, request: WebcamSnapshotRequest) -> None:
        import simplyprint_ws_client.shared.sp.simplyprint_api as sp_api

        self.logger.info(
            f'Sending webcam snapshot id: {request.snapshot_id} endpoint: {request.endpoint or "Simplyprint"}',
        )
        await sp_api.SimplyPrintApi.post_snapshot(
            snapshot_id=request.snapshot_id,
            image_data=image,
            endpoint=request.endpoint,
        )

    async def _fetch_webcam_image(self) -> bytes:
        try:
            raw_data = await asyncio.wait_for(self._webcam_frame.get(), timeout=60)
        except asyncio.TimeoutError:
            self.logger.debug("Timeout while fetching webcam image")
            return None

        img = iio.imread(
            uri=raw_data,
            extension='.jpeg',
            index=None,
        )

        jpg_encoded = iio.imwrite("<bytes>", img, extension=".jpeg")
        # rotated_img = PIL.Image.open(io.BytesIO(jpg_encoded))
        # rotated_img.rotate(270)
        # rotated_img.thumbnail((720, 720), resample=PIL.Image.Resampling.LANCZOS)
        # bytes_array = io.BytesIO()
        # rotated_img.save(bytes_array, format='JPEG')
        # jpg_encoded = bytes_array.getvalue()

        return jpg_encoded

    async def _handle_multipart_content(self, response: aiohttp.ClientResponse) -> None:
        reader = aiohttp.MultipartReader.from_response(response)
        async for part in reader:
            if part.headers[aiohttp.hdrs.CONTENT_TYPE] != 'image/jpeg':
                continue
            content = await part.read()
            if self._webcam_frame.full():
                await self._webcam_frame.get()
            await self._webcam_frame.put(memoryview(content))

            if self._is_stopped or self._webcam_distribution_task_handle is None:
                break
            # max framerate of SP is 2fps
            await asyncio.sleep(1 / 4)

    async def _handle_image_content(self, response: aiohttp.ClientResponse) -> None:
        content = await response.read()
        if self._webcam_frame.full():
            await self._webcam_frame.get()
        await self._webcam_frame.put(memoryview(content))

    @async_task
    async def _webcam_receive_task(self) -> None:
        self.logger.debug('Webcam receive task started')

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=0,  # Disable total timeout
                connect=30,
                sock_read=0,
                sock_connect=30,
                ),
        ) as session:
            while not self._is_stopped and self._webcam_distribution_task_handle is not None:
                try:
                    async with session.get(self.config.webcam_uri) as response:
                        if response.headers['Content-Type'] == 'image/jpeg':
                            await self._handle_image_content(response)
                        elif 'multipart' in response.headers['Content-Type'].lower():
                            await self._handle_multipart_content(response)
                        else:
                            self.logger.debug(
                                'Unsupported content type: {!s}'.format(response.headers['Content-Type']),
                            )
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    self.logger.debug('Failed to fetch webcam image')
                    await asyncio.sleep(10)

    @async_task
    async def _webcam_distribution_task(self) -> None:
        self.logger.debug('Webcam distribution task started')

        # Start the webcam receive task
        await self._webcam_receive_task()

        while not self._is_stopped and time.time() < self._webcam_timeout:
            try:
                if self._requested_webcam_snapshots.qsize() > 0:
                    request = await self._requested_webcam_snapshots.get()
                    if request.snapshot_id is not None:
                        image = await self._fetch_webcam_image()
                        await self._send_webcam_snapshot_to_endpoint(image=image, request=request)
                        continue
                    if self.printer.intervals.is_ready('webcam'):
                        image = await self._fetch_webcam_image()
                        await self._send_webcam_snapshot(image=image)
                    else:
                        await self._requested_webcam_snapshots.put(request)
                        await self.printer.intervals.wait_for('webcam')
                else:
                    await asyncio.sleep(0.1)
                # else drop the frame and grab the next one
            except Exception:
                self.logger.exception("Failed to distribute webcam image")
                await asyncio.sleep(10)
        self._webcam_distribution_task_handle = None

    async def on_webcam_snapshot(
        self,
        event: WebcamSnapshotDemandData,
    ) -> None:
        """Take a snapshot from the webcam."""
        # From Javad
        # There is an edge case for the `WebcamSnapshotEvent` where and `id` and optional `endpoint` can be provided,
        # in which case a request to a HTTP endpoint can be sent, the library implements
        # `SimplyPrintApi.post_snapshot` you can call if you want to implement job state images.

        self._webcam_timeout = time.time() + 10
        if self._webcam_distribution_task_handle is None and self.config.webcam_uri is not None:
            self._webcam_distribution_task_handle = await self._webcam_distribution_task()

        request = WebcamSnapshotRequest(snapshot_id=event.id, endpoint=event.endpoint)
        await self._requested_webcam_snapshots.put(request)

    async def on_stream_off(self) -> None:
        """Turn off the webcam stream."""
        pass

    async def on_api_restart(self) -> None:
        """Restart the API."""
        self.logger.info("Restarting API")
        # the api is running as a systemd service, so we can just restart the service
        # by terminating the process
        raise KeyboardInterrupt()

    async def _send_mesh_data(self) -> None:
        compensation = self.duet.om['move']['compensation']
        self.logger.debug('Send mesh data called')
        self.logger.debug('Compensation: {!s}'.format(compensation))

        # move.compensation.file
        # if is not none - mesh is loaded

        # M409 K"move.compensation.liveGrid"
        # {
        #     "key": "move.compensation.liveGrid",
        #     "flags": "",
        #     "result": {
        #         "axes": [
        #             "X",
        #             "Y"
        #         ],
        #         "maxs": [
        #             842.4,
        #             379.5
        #         ],
        #         "mins": [
        #             8.6,
        #             25.5
        #         ],
        #         "radius": -1,
        #         "spacings": [
        #             49,
        #             50.6
        #         ]
        #     }
        # }

        # the mesh data is stored in an csv file on the duet board
        # we need to download the file and send it to simplyprint
        # the format is as follows:
        # first row contains a comment
        # second row contains the headers for the mesh shape
        # third row contains the data for the mesh shape
        # consecutive rows contain the mesh data as matrix

        self.logger.debug('Downloading mesh data from duet')
        heightmap = io.BytesIO()

        async for chunk in self.duet.api.rr_download(filepath=compensation['file']):
            heightmap.write(chunk)

        heightmap.seek(0)
        heightmap = heightmap.read().decode('utf-8')

        self.logger.debug('Mesh data: {!s}'.format(heightmap))

        mesh_data_csv = csv.reader(heightmap.splitlines()[3:], dialect='unix')

        mesh_data = []

        z_min = 100
        z_max = 0

        for row in mesh_data_csv:
            x_line = []
            for x in row:
                value = float(x.strip())
                z_min = min(z_min, value)
                z_max = max(z_max, value)
                x_line.append(value)
            mesh_data.append(x_line)

        bed = {
            # volume.formFactor 0..1 string The form factor of the printer’s bed,
            # valid values are “rectangular” and “circular”
            'type': 'rectangular' if compensation['liveGrid']['radius'] == -1 else 'circular',
            'x_min': compensation['liveGrid']['mins'][0],
            'x_max': compensation['liveGrid']['maxs'][0],
            'y_min': compensation['liveGrid']['mins'][1],
            'y_max': compensation['liveGrid']['maxs'][1],
            'z_min': z_min,
            'z_max': z_max,
        }

        data = {
            'mesh_min': [bed['y_min'], bed['x_min']],
            'mesh_max': [bed['y_max'], bed['x_max']],
            'mesh_matrix': mesh_data,
        }

        self.logger.debug('Mesh data: {!s}'.format(data))

        # mesh data is matrix of y,x and z
        await self.send(
            MeshDataMsg(data=data),
        )
