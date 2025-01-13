"""A virtual client for the SimplyPrint.io Service."""

import asyncio
import base64
import csv
import io
import pathlib
import platform
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

from simplyprint_ws_client.client.client import Client, ClientConfigChangedEvent, DefaultClient
from simplyprint_ws_client.client.config import PrinterConfig
from simplyprint_ws_client.client.protocol import ClientEvents, Demands, Events
from simplyprint_ws_client.client.state.printer import FileProgressState, PrinterFilamentSensorEnum, PrinterStatus
from simplyprint_ws_client.const import VERSION as SP_VERSION
from simplyprint_ws_client.helpers.file_download import FileDownload
from simplyprint_ws_client.helpers.intervals import IntervalTypes
from simplyprint_ws_client.helpers.physical_machine import PhysicalMachine

from yarl import URL

from . import __version__
from .duet.api import RepRapFirmware
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


def merge_dictionary(source, destination):
    """Merge multiple dictionaries."""
    # {'a': 1, 'b': {'c': 2}},
    # {'b': {'c': 3}},
    # {'a': 1, 'b': {'c': 3}}

    result = {}
    dk = dict(destination)
    for key, value in source.items():
        if isinstance(value, dict):
            result[key] = merge_dictionary(value, destination.get(key, {}))
        elif isinstance(value, list):
            result[key] = value
            dest_value = destination.get(key, [])
            src_len = len(value)
            dest_len = len(dest_value)
            if dest_len == 0:
                result[key] = value
                continue
            if src_len > dest_len:
                raise ValueError(
                    "List length mismatch in merge for key: {!s} src: {!s} dest: {!s}".format(key, value, dest_value),
                )
            if src_len < dest_len:
                result[key] = dest_value
                continue

            for idx, item in enumerate(value):
                if dest_value[idx] is None:
                    continue
                if isinstance(item, dict):
                    result[key][idx] = merge_dictionary(item, dest_value[idx])
        else:
            result[key] = destination.get(key, value)
        dk.pop(key, None)
    result.update(dk)
    return result


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

    duet: RepRapFirmware

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the client."""
        super().__init__(*args, **kwargs)

        self.duet = RepRapFirmware(
            address=self.config.duet_uri,
            password=self.config.duet_password,
            logger=self.logger,
        )

        self._duet_connected = False
        self._printer_timeout = 0
        self._printer_status = None
        self._job_status = None
        self._compensation = None

        self._webcam_timeout = 0
        self._webcam_task_handle = None
        self._requested_webcam_snapshots = asyncio.Queue()

        self._background_task = set()
        self._is_stopped = False

    async def init(self) -> None:
        """Initialize the client."""
        self._printer_timeout = time.time() + 60 * 5  # 5 minutes

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

    @Client.connected.getter
    def connected(self) -> bool:
        """Check if the client is connected to the server."""
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        # if value is False:
        #     asyncio.run_coroutine_threadsafe(self.stop(), self.event_loop)
        # else:
        Client.connected.fset(self, value)

    @Events.ConnectEvent.on
    async def on_connect(self, event: Events.ConnectEvent) -> None:
        """Connect to Simplyprint.io."""
        self.logger.info('Connected to Simplyprint.io')

        await self._printer_status_task()
        await self._job_status_task()
        await self._filament_monitors_task()
        await self._mesh_compensation_status_task()
        await self._connector_status_task()

    @Events.PrinterSettingsEvent.on
    async def on_printer_settings(
        self,
        event: Events.PrinterSettingsEvent,
    ) -> None:
        """Update the printer settings."""
        self.logger.debug("Printer settings: %s", event.data)

    @Demands.GcodeEvent.on
    async def on_gcode(self, event: Demands.GcodeEvent) -> None:
        """
        Receive GCode from SP and send GCode to duet.

        The GCode is checked for allowed commands and then sent to the Duet.
        """
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
                response.append(await self.duet.rr_gcode(item.compress()))
            elif item.code == 'M300' and self.config.in_setup:
                response.append(
                    await self.duet.rr_gcode(
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

    def _upload_file_progress(self, progress: float) -> None:
        """Update the file upload progress."""
        # contrains the progress from 50 - 90 %
        self.printer.file_progress.percent = min(round(50 + (max(0, min(50, progress / 2))), 0), 90.0)
        # Ensure we send events to SimplyPrint
        asyncio.run_coroutine_threadsafe(self.consume_state(), self.event_loop)

    async def _auto_start_file(self, event) -> None:
        """Auto start the file after it has been uploaded."""
        self.printer.job_info.filename = event.file_name
        timeout = time.time() + 400  # seconds

        while timeout > time.time():
            try:
                response = await self.duet.rr_fileinfo(
                    name="0:/gcodes/{!s}".format(event.file_name),
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            except TimeoutError:
                response = {'err': 1}  # timeout

            if response['err'] == 0:
                break

            timeleft = 10 - ((timeout - time.time()) * 0.025)
            self.printer.file_progress.percent = min(99.9, (90.0 + timeleft))

            # Ensure we send events to SimplyPrint
            asyncio.run_coroutine_threadsafe(
                self.consume_state(),
                self.event_loop,
            )
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

        This task ensures that file upload progress is sent every 10 seconds to prevent
        timeouts on clients with low bandwidth. The progress step between 0.5% can exceed
        the default timeout of 30 seconds, so frequent updates are necessary.
        """
        while not self._is_stopped and self.printer.file_progress.state == FileProgressState.DOWNLOADING:
            file_progress_event = self.printer.file_progress.get_field_event('percent')
            if file_progress_event is not None:
                self.printer.mark_event_as_dirty(file_progress_event)
            await asyncio.sleep(10)

    @async_task
    @async_supress
    async def _download_file_from_sp_and_upload_to_duet(
        self,
        event: Demands.FileEvent,
    ) -> None:
        """Download a file from Simplyprint.io and upload it to the printer."""
        downloader = FileDownload(self)

        self.printer.file_progress.state = FileProgressState.DOWNLOADING
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
                    response = await self.duet.rr_upload_stream(
                        filepath='{!s}{!s}'.format(prefix, event.file_name),
                        file=f,
                        progress=self._upload_file_progress,
                    )
                    if response['err'] != 0:
                        self.printer.file_progress.state = FileProgressState.ERROR
                        return
                    break
                except aiohttp.ClientResponseError as e:
                    if e.status == 401 or e.status == 500:
                        await self.duet.reconnect()
                    else:
                        # Ensure the exception is not supressed
                        raise e
                finally:
                    retries -= 1

        if event.auto_start:
            await self._auto_start_file(event)

        self.printer.file_progress.percent = 100.0
        self.printer.file_progress.state = FileProgressState.READY

    @Demands.FileEvent.on
    async def on_file(self, event: Demands.FileEvent) -> None:
        """Download a file from Simplyprint.io to the printer."""
        await self._download_file_from_sp_and_upload_to_duet(event=event)

    @Demands.StartPrintEvent.on
    async def on_start_print(self, _) -> None:
        """Start the print job."""
        await self.duet.rr_gcode(
            'M23 "0:/gcodes/{!s}"'.format(self.printer.job_info.filename),
        )
        await self.duet.rr_gcode('M24')

    @Demands.PauseEvent.on
    async def on_pause_event(self, _) -> None:
        """Pause the print job."""
        await self.duet.rr_gcode('M25')

    @Demands.ResumeEvent.on
    async def on_resume_event(self, _) -> None:
        """Resume the print job."""
        await self.duet.rr_gcode('M24')

    @Demands.CancelEvent.on
    async def on_cancel_event(self, _) -> None:
        """Cancel the print job."""
        await self.duet.rr_gcode('M25')
        await self.duet.rr_gcode('M0')

    async def _connect_to_duet(self) -> None:
        """Connect to the Duet board."""
        try:
            response = await self.duet.connect()
            self.logger.debug("Response from Duet: {!s}".format(response))
            self._printer_status = None
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            self.printer.status = PrinterStatus.OFFLINE
            raise e
        except aiohttp.ClientError as e:
            self.logger.debug(
                "Failed to connect to Duet with error: {!s}".format(e),
            )
            raise e

        try:
            board = await self.duet.rr_model(key='boards[0]')
            board = board['result']
        except Exception as e:
            self.logger.error('Error connecting to Duet Board: {0}'.format(e))
            raise e

        self.logger.info('Connected to Duet Board {0}'.format(board))

        if self.config.duet_unique_id is None:
            # Set the unique ID if it is not set
            # and emit an event to notify the client
            # that the configuration has changed
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

        self.printer.firmware.name = board['firmwareName']
        self.printer.firmware.version = board['firmwareVersion']
        self.set_api_info("meltingplot.duet-simplyprint-connector", __version__)
        self.set_ui_info("meltingplot.duet-simplyprint-connector", __version__)

        self._duet_connected = True

    async def _update_temperatures(self, printer_status: dict) -> None:
        """Update the printer temperatures."""
        heaters = printer_status['result']['heat']['heaters']
        # TODO make it aware of more than one bed heater
        bed_heater_index = printer_status['result']['heat']['bedHeaters'][0]

        self.printer.bed_temperature.actual = heaters[bed_heater_index]['current']
        if heaters[0]['state'] != 'off':
            self.printer.bed_temperature.target = heaters[bed_heater_index]['active']
        else:
            self.printer.bed_temperature.target = 0.0

        for tool_idx, tool_temperature in enumerate(self.printer.tool_temperatures):
            heater_idx = printer_status['result']['tools'][tool_idx]['heaters'][0]
            tool_temperature.actual = heaters[heater_idx]['current']

            if heaters[1]['state'] != 'off':
                tool_temperature.target = heaters[heater_idx]['active']
            else:
                tool_temperature.target = 0.0

        self.printer.ambient_temperature.ambient = 20

    async def _fetch_partial_status(self, *args, **kwargs) -> dict:
        response = await self.duet.rr_model(
            *args,
            **kwargs,
        )
        return response

    async def _fetch_full_status(self) -> dict:
        full_status = {}

        keys = await self.duet.rr_model(
            key='',
            depth=1,
            frequently=False,
        )

        for key in keys['result']:
            response = await self._fetch_partial_status(
                key=key,
                depth=99,
                frequently=False,
            )
            full_status[key] = response['result']

        full_status = {'result': full_status}
        return full_status

    async def _fetch_printer_status(self, force_full_status=False) -> dict:
        try:
            if self._printer_status is None or force_full_status:
                printer_status = await self._fetch_full_status()
            else:
                partial_status = await self._fetch_partial_status(
                    key='',
                    frequently=True,
                    depth=99,
                )

                printer_status = merge_dictionary(self._printer_status, partial_status)

        except (
            aiohttp.ClientConnectionError,
            TimeoutError,
            asyncio.exceptions.TimeoutError,
        ):
            printer_status = None
        except Exception:
            self.logger.exception(
                "An exception occurred while updating the printer status",
            )
            # use old printer status if new one is not available
            printer_status = self._printer_status
        return printer_status

    async def _fetch_job_status(self) -> dict:
        job_status = await self._fetch_rr_model(
            key='job',
            return_on_exception=self._job_status,
            frequently=False,
            depth=5,
        )
        return job_status

    async def _fetch_rr_model(self, key, return_on_exception=None, return_on_timeout=None, **kwargs) -> dict:
        try:
            response = await self.duet.rr_model(
                key=key,
                **kwargs,
            )
        except (
            aiohttp.ClientConnectionError,
            TimeoutError,
            asyncio.exceptions.TimeoutError,
        ):
            response = return_on_timeout
        except Exception as e:
            msg = "An {!s} exception occurred while fetching rr_model with key: {!s}".format(
                type(e).__name__,
                key,
            )
            self.logger.exception(msg)
            response = return_on_exception
        return response

    @async_task
    async def _printer_status_task(self) -> None:
        """Task to check for printer status changes and send printer data to SimplyPrint."""
        next_full_status = 0
        while not self._is_stopped:
            try:
                if not self._duet_connected:
                    try:
                        await self._connect_to_duet()
                    except Exception:
                        await asyncio.sleep(60)
                        continue

                    if self.config.in_setup:
                        await self.duet.rr_gcode(
                            f'M291 P"Code: {self.config.short_id}" R"Simplyprint.io Setup" S2',
                        )

                fetch_full_status = False
                if time.time() > next_full_status:
                    # Fetch full status every 15 minutes
                    fetch_full_status = True
                    next_full_status = time.time() + 60 * 15

                self._printer_status = await self._fetch_printer_status(force_full_status=fetch_full_status)
                await self._update_printer_status()
                await asyncio.sleep(1)
            except asyncio.CancelledError as e:
                await self.duet.close()
                raise e
            except Exception:
                continue

    @async_task
    async def _job_status_task(self) -> None:
        """Task to check for job status changes and send job data to SimplyPrint."""
        while not self._is_stopped:
            if not self._duet_connected:
                await asyncio.sleep(60)
                continue

            self._job_status = await self._fetch_job_status()

            if self.printer.status != PrinterStatus.OFFLINE:
                if await self._is_printing():
                    await self._update_job_info()

            await asyncio.sleep(5)

    @async_task
    async def _mesh_compensation_status_task(self) -> None:
        """Task to check for mesh compensation changes and send mesh data to SimplyPrint."""
        while not self._is_stopped:
            if not self._duet_connected:
                await asyncio.sleep(60)
                continue

            compensation = await self._fetch_rr_model(
                key='move.compensation',
                return_on_exception=None,
                frequently=False,
                depth=4,
            )

            old_compensation = self._compensation
            self._compensation = compensation

            if (
                compensation is not None and 'file' in compensation['result']
                and compensation['result']['file'] is not None and (
                    old_compensation is None or 'file' not in old_compensation['result']
                    or old_compensation['result']['file'] != compensation['result']['file']
                )
            ):
                try:
                    await self._send_mesh_data()
                except Exception as e:
                    self.logger.exception(
                        "An exception occurred while sending mesh data",
                        exc_info=e,
                    )
            await asyncio.sleep(10)

    async def _update_cpu_and_memory_info(self) -> None:
        self.printer.cpu_info.usage = psutil.cpu_percent(interval=1)
        try:
            self.printer.cpu_info.temperature = psutil.sensors_temperatures()['coretemp'][0].current
        except KeyError:
            self.printer.cpu_info.temperature = 0.0
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

    async def _map_duet_state_to_printer_status(self, printer_status: dict) -> None:
        try:
            printer_state = printer_status['result']['state']['status']
        except (KeyError, TypeError):
            printer_state = 'disconnected'

        # SP is sensitive to printer status while printing
        # so we need to differentiate between printer status while printing and not printing
        if await self._is_printing():
            self.printer.status = duet_state_simplyprint_status_while_printing_mapping[printer_state]
        else:
            self.printer.status = duet_state_simplyprint_status_mapping[printer_state]

    @async_task
    async def _filament_monitors_task(self) -> None:
        """Task to check for filament sensor changes."""
        while not self._is_stopped:
            if not self._duet_connected:
                await asyncio.sleep(10)
                continue

            filament_monitors = await self._fetch_rr_model(
                key='sensors.filamentMonitors',
                frequently=False,
                depth=4,
            )

            await self._update_filament_sensor(filament_monitors)
            await asyncio.sleep(10)

    async def _update_filament_sensor(self, filament_monitors: dict) -> None:
        if filament_monitors is None:
            return

        filament_monitors = filament_monitors['result']

        for monitor in filament_monitors:
            if monitor['enableMode'] > 0:
                self.printer.settings.has_filament_sensor = True
                if monitor['status'] == 'ok':
                    self.printer.filament_sensor.state = PrinterFilamentSensorEnum.LOADED
                else:
                    self.printer.filament_sensor.state = PrinterFilamentSensorEnum.RUNOUT
                    break  # only one sensor is needed

                try:
                    if monitor['calibrated'] is None or self.printer.status != PrinterStatus.PAUSED:
                        continue
                    if monitor['calibrated']['percentMin'] < monitor['configured']['percentMin']:
                        self.printer.filament_sensor.state = PrinterFilamentSensorEnum.RUNOUT
                        break  # only one sensor is needed
                    if monitor['calibrated']['percentMax'] < monitor['configured']['percentMax']:
                        self.printer.filament_sensor.state = PrinterFilamentSensorEnum.RUNOUT
                        break  # only one sensor is needed
                except KeyError:
                    pass

    async def _update_printer_status(self) -> None:
        printer_status = self._printer_status

        if printer_status is None:
            if time.time() > self._printer_timeout:
                self.printer.status = PrinterStatus.OFFLINE
                self._duet_connected = False
            return

        try:
            await self._update_temperatures(printer_status)
        except KeyError:
            self.printer.bed_temperature.actual = 0.0
            self.printer.tool_temperatures[0].actual = 0.0

        old_printer_state = self.printer.status
        await self._map_duet_state_to_printer_status(printer_status)

        if self.printer.status == PrinterStatus.CANCELLING and old_printer_state == PrinterStatus.PRINTING:
            self.printer.job_info.cancelled = True
        elif self.printer.status == PrinterStatus.OPERATIONAL:  # The machine is on but has nothing to do
            if self.printer.job_info.started or old_printer_state == PrinterStatus.PRINTING:
                # setting 'finished' will clear 'started'
                self.printer.job_info.finished = True
                self.printer.job_info.progress = 100.0

        self._printer_timeout = time.time() + 60 * 5  # 5 minutes

    async def _is_printing(self) -> bool:
        printing = (
            self.printer.status == PrinterStatus.PRINTING or self.printer.status == PrinterStatus.PAUSED
            or self.printer.status == PrinterStatus.PAUSING or self.printer.status == PrinterStatus.RESUMING
        )

        job_status = self._job_status

        if (job_status is None or 'result' not in job_status or 'file' not in job_status['result']):
            return printing

        job_status = job_status['result']['file']

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
        if self._job_status is None:
            return

        job_status = self._job_status['result']

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

    async def tick(self) -> None:
        """Update the client state."""
        try:
            await self.send_ping()
        except Exception as e:
            self.logger.exception(
                "An exception occurred while ticking the client state",
                exc_info=e,
            )

    async def stop(self) -> None:
        """Stop the client."""
        self.logger.debug('Stopping client')
        self._is_stopped = True
        for task in self._background_task:
            task.cancel()
        await self.duet.close()

    @Demands.WebcamTestEvent.on
    async def on_webcam_test(self, event: Demands.WebcamTestEvent) -> None:
        """Test the webcam."""
        self.printer.webcam_info.connected = (True if self.config.webcam_uri is not None else False)

    async def _send_webcam_snapshot(self, image: bytes) -> None:
        jpg_encoded = image
        base64_encoded = base64.b64encode(jpg_encoded).decode()
        await self.send_event(
            ClientEvents.StreamEvent(data={"base": base64_encoded}),
        )

    async def _send_webcam_snapshot_to_endpoint(self, image: bytes, request: WebcamSnapshotRequest) -> None:
        import simplyprint_ws_client.helpers.simplyprint_api as sp_api

        self.logger.info(f'Sending webcam snapshot to endpoint: {request.endpoint}')
        await sp_api.SimplyPrintApi.post_snapshot(
            endpoint=request.endpoint,
            image_data=image,
            snapshot_id=request.snapshot_id,
        )

    async def _fetch_webcam_image(self) -> bytes:
        headers = {"Accept": "image/jpeg"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                url=self.config.webcam_uri,
                headers=headers,
            ) as r:
                raw_data = await r.read()

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

    @async_task
    async def _webcam_task(self) -> None:
        self.logger.debug('Webcam task started')
        while time.time() < self._webcam_timeout:
            try:
                image = await self._fetch_webcam_image()

                if self._requested_webcam_snapshots.qsize() > 0:
                    request = await self._requested_webcam_snapshots.get()
                    if request.endpoint is not None:
                        await self._send_webcam_snapshot_to_endpoint(image=image, request=request)
                        continue
                    if self.intervals.is_ready(IntervalTypes.WEBCAM):
                        await self._send_webcam_snapshot(image=image)
                    else:
                        await self._requested_webcam_snapshots.put(request)
            except Exception as e:
                self.logger.debug("Failed to fetch webcam image: {}".format(e))
                await asyncio.sleep(10)
        self._webcam_task_handle = None

    @Demands.WebcamSnapshotEvent.on
    async def on_webcam_snapshot(
        self,
        event: Demands.WebcamSnapshotEvent,
    ) -> None:
        """Take a snapshot from the webcam."""
        # From Javad
        # There is an edge case for the `WebcamSnapshotEvent` where and `id` and optional `endpoint` can be provided,
        # in which case a request to a HTTP endpoint can be sent, the library implements
        # `SimplyPrintApi.post_snapshot` you can call if you want to implement job state images.

        self._webcam_timeout = time.time() + 60
        if self._webcam_task_handle is None and self.config.webcam_uri is not None:
            self._webcam_task_handle = await self._webcam_task()

        request = WebcamSnapshotRequest(snapshot_id=event.id, endpoint=event.endpoint)
        await self._requested_webcam_snapshots.put(request)

    @Demands.StreamOffEvent.on
    async def on_stream_off(self, event: Demands.StreamOffEvent) -> None:
        """Turn off the webcam stream."""
        pass

    @Demands.ApiRestartEvent.on
    async def on_api_restart(self, event: Demands.ApiRestartEvent) -> None:
        """Restart the API."""
        self.logger.info("Restarting API")
        # the api is running as a systemd service, so we can just restart the service
        # by terminating the process
        raise KeyboardInterrupt()

    async def _send_mesh_data(self) -> None:
        compensation = self._compensation['result']
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

        async for chunk in self.duet.rr_download(filepath=compensation['file']):
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
        await self.send_event(
            ClientEvents.MeshDataEvent(data=data),
        )
