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
from typing import Optional

import aiohttp

import imageio.v3 as iio

import psutil

from simplyprint_ws_client.client.client import ClientConfigChangedEvent, DefaultClient
from simplyprint_ws_client.client.config import PrinterConfig
from simplyprint_ws_client.client.protocol import ClientEvents, Demands, Events
from simplyprint_ws_client.client.state.printer import FileProgressState, PrinterFilamentSensorEnum, PrinterStatus
from simplyprint_ws_client.const import VERSION as SP_VERSION
from simplyprint_ws_client.helpers.file_download import FileDownload
from simplyprint_ws_client.helpers.intervals import IntervalTypes
from simplyprint_ws_client.helpers.physical_machine import PhysicalMachine

from . import __version__
from .duet.api import RepRapFirmware
from .gcode import GCodeBlock

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
    'simulating': PrinterStatus.NOT_READY,
    'busy': PrinterStatus.NOT_READY,
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


@dataclass
class VirtualConfig(PrinterConfig):
    """Configuration for the VirtualClient."""

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
        self._requested_webcam_snapshots = 1
        self._requested_webcam_snapshots_lock = asyncio.Lock()

        self._background_task = set()
        self._is_stopped = False

    async def init(self) -> None:
        """Initialize the client."""
        self._printer_timeout = time.time() + 60 * 5  # 5 minutes

        await self._printer_status_task()
        await self._job_status_task()
        await self._filament_monitors_task()
        await self._compensation_status_task()
        await self._connector_status_task()

        self.printer.info.core_count = psutil.cpu_count(logical=False)
        self.printer.info.total_memory = psutil.virtual_memory().total
        self.printer.info.hostname = socket.getfqdn()
        self.printer.info.os = "Meltingplot Duet Connector v{!s}".format(__version__)
        self.printer.info.sp_version = SP_VERSION
        self.printer.info.python_version = platform.python_version()
        self.printer.info.machine = PhysicalMachine.machine()

    @Events.ConnectEvent.on
    async def on_connect(self, event: Events.ConnectEvent) -> None:
        """Connect to Simplyprint.io."""
        self.logger.info('Connected to Simplyprint.io')

    @Events.PrinterSettingsEvent.on
    async def on_printer_settings(
        self,
        event: Events.PrinterSettingsEvent,
    ) -> None:
        """Update the printer settings."""
        self.logger.debug("Printer settings: %s", event.data)

    @Demands.GcodeEvent.on
    async def on_gcode(self, event: Demands.GcodeEvent) -> None:
        """Send GCode to the printer."""
        self.logger.debug("Gcode: {!r}".format(event.list))

        gcode = GCodeBlock().parse(event.list)
        self.logger.debug("Gcode: {!r}".format(gcode))

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
            if item.code in allowed_commands:
                response.append(await self.duet.rr_gcode(item.compress()))
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
                # the api is running as a systemd service, so we can just restart the service
                # by terminating the process
                raise KeyboardInterrupt()
            else:
                response.append('{!s} G-Code blocked'.format(item.code))

            # await self.send_event(
            #    ClientEvents.Ter(
            #        data={
            #            "response": response}
            #    ))

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

    def _file_progress(self, progress: float) -> None:
        # contrains the progress from 50 - 90 %
        self.printer.file_progress.percent = min(round(50 + (max(0, min(50, progress / 2))), 0), 90.0)
        # Ensure we send events to SimplyPrint
        asyncio.run_coroutine_threadsafe(self.consume_state(), self.event_loop)

    async def _auto_start_file(self, event) -> None:
        self.printer.job_info.filename = event.file_name
        timeout = time.time() + 400  # 400 seconds
        # 10 % / 400 seconds
        while timeout > time.time():
            try:
                response = await self.duet.rr_fileinfo(
                    name="0:/gcodes/{!s}".format(event.file_name),
                    timeout=aiohttp.ClientTimeout(total=5),
                )
            except TimeoutError:
                pass

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

    async def _download_and_upload_file(
        self,
        event: Demands.FileEvent,
    ) -> None:
        downloader = FileDownload(self)

        self.printer.file_progress.state = FileProgressState.DOWNLOADING
        self.printer.file_progress.percent = 0.0

        with tempfile.NamedTemporaryFile(suffix='.gcode') as f:
            async for chunk in downloader.download(
                url=event.url,
                clamp_progress=(lambda x: int(max(0, min(50, x / 2)))),
            ):
                f.write(chunk)

            f.seek(0)
            prefix = '0:/gcodes/'
            retries = 3
            while retries > 0:
                try:
                    response = await self.duet.rr_upload_stream(
                        filepath='{!s}{!s}'.format(prefix, event.file_name),
                        file=f,
                        progress=self._file_progress,
                    )
                    if response['err'] != 0:
                        self.printer.file_progress.state = FileProgressState.ERROR
                        return
                    break
                except aiohttp.ClientResponseError as e:
                    if e.status == 401:
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

    @async_task
    async def _download_and_upload_file_task(
        self,
        event: Demands.FileEvent,
    ) -> None:
        try:
            await self._download_and_upload_file(event)
        except Exception as e:
            self.logger.exception(
                "An exception occurred while downloading and uploading a file",
                exc_info=e,
            )

    @Demands.FileEvent.on
    async def on_file(self, event: Demands.FileEvent) -> None:
        """Download a file from Simplyprint.io to the printer."""
        await self._download_and_upload_file_task(event=event)

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
        try:
            response = await self.duet.connect()
            self.logger.debug("Response from Duet: {!s}".format(response))
        except (aiohttp.ClientConnectionError, TimeoutError):
            self.printer.status = PrinterStatus.OFFLINE
        except aiohttp.ClientError as e:
            self.logger.debug(
                "Failed to connect to Duet with error: {!s}".format(e),
            )

        try:
            board = await self.duet.rr_model(key='boards[0]')
            board = board['result']
        except Exception as e:
            self.logger.error('Error connecting to Duet Board: {0}'.format(e))

        self.logger.info('Connected to Duet Board {0}'.format(board))

        if self.config.duet_unique_id is None:
            self.config.duet_unique_id = board['uniqueId']
            await self.event_bus.emit(ClientConfigChangedEvent)
        else:
            if self.config.duet_unique_id != board['uniqueId']:
                self.logger.error(
                    'Unique ID mismatch: {0} != {1}'.format(self.config.duet_unique_id, board['uniqueId']),
                )
                self.printer.status = PrinterStatus.OFFLINE
                # TODO: Implement a search mechanism based on the unique ID
                return

        self.printer.firmware.name = board['firmwareName']
        self.printer.firmware.version = board['firmwareVersion']
        self.set_api_info("meltingplot.duet-simplyprint-connector", __version__)
        self.set_ui_info("meltingplot.duet-simplyprint-connector", __version__)
        self._duet_connected = True

    async def _update_temperatures(self, printer_status: dict) -> None:
        heaters = printer_status['result']['heat']['heaters']
        self.printer.bed_temperature.actual = heaters[0]['current']
        if heaters[0]['state'] != 'off':
            self.printer.bed_temperature.target = heaters[0]['active']
        else:
            self.printer.bed_temperature.target = 0.0

        self.printer.tool_temperatures[0].actual = heaters[1]['current']

        if heaters[1]['state'] != 'off':
            self.printer.tool_temperatures[0].target = heaters[1]['active']
        else:
            self.printer.tool_temperatures[0].target = 0.0

        self.printer.ambient_temperature.ambient = 20

    async def _fetch_printer_status(self) -> dict:
        try:
            printer_status = await self.duet.rr_model(
                key='',
                frequently=True,
            )
        except (aiohttp.ClientConnectionError, TimeoutError):
            printer_status = None
        except KeyboardInterrupt as e:
            raise e
        except Exception:
            self.logger.exception(
                "An exception occurred while updating the printer status",
            )
            # use old printer status if new one is not available
            printer_status = self._printer_status
        return printer_status

    async def _fetch_job_status(self) -> dict:
        try:
            job_status = await self.duet.rr_model(
                key='job',
                frequently=False,
                depth=5,
            )
        except (aiohttp.ClientConnectionError, TimeoutError):
            job_status = None
        except KeyboardInterrupt as e:
            raise e
        except Exception:
            self.logger.exception(
                "An exception occurred while updating the job info",
            )
            # use old job status if new one is not available
            job_status = self._job_status
        return job_status

    async def _fetch_rr_model(self, key, **kwargs) -> dict:
        try:
            response = await self.duet.rr_model(
                key=key,
                **kwargs,
            )
        except (aiohttp.ClientConnectionError, TimeoutError):
            response = None
        except KeyboardInterrupt as e:
            raise e
        except Exception:
            self.logger.exception(
                "An exception occurred while fetching rr_model with key: {!s}".fomrat(key),
            )
        return response

    @async_task
    async def _printer_status_task(self) -> None:
        """Task to check for printer status changes and send printer data to SimplyPrint."""
        while not self._is_stopped:
            try:
                if not self._duet_connected:
                    await self._connect_to_duet()
            except KeyboardInterrupt as e:
                raise e
            except Exception:
                await asyncio.sleep(60)
                continue

            self._printer_status = await self._fetch_printer_status()
            await self._update_printer_status()

            await asyncio.sleep(1)

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
    async def _compensation_status_task(self) -> None:
        """Task to check for mesh compensation changes and send mesh data to SimplyPrint."""
        while not self._is_stopped:
            if not self._duet_connected:
                await asyncio.sleep(60)
                continue

            try:
                compensation = await self._fetch_rr_model(
                    key='move.compensation',
                    frequently=False,
                    depth=4,
                )
            except KeyboardInterrupt as e:
                raise e
            except Exception:
                compensation = None

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

    async def _get_local_ip_and_mac(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            # doesn't even have to be reachable
            # we just need to know the local ip
            # so we can send it to simplyprint
            s.connect(("168.119.98.102", 80))
            local_ip = s.getsockname()[0]
        except socket.error:
            local_ip = '127.0.0.1'
        finally:
            s.close()
        self.printer.info.local_ip = local_ip

        nics = psutil.net_if_addrs()
        for iface in nics:
            if iface == 'lo':
                continue
            mac = None
            found = False
            for addr in nics[iface]:
                if addr.family == socket.AF_INET and addr.address == local_ip:
                    found = True
                if addr.family == psutil.AF_LINK:
                    mac = addr.address
            if found:
                self.printer.info.mac = mac

    @async_task
    async def _connector_status_task(self) -> None:
        """Task to gather connector infos and send data to SimplyPrint."""
        while not self._is_stopped:
            await self._update_cpu_and_memory_info()
            await self._get_local_ip_and_mac()
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
            try:
                filament_monitors = await self.duet.rr_model(
                    key='sensors.filamentMonitors',
                    frequently=False,
                    depth=4,
                )
            except KeyboardInterrupt as e:
                raise e
            except Exception:
                filament_monitors = None

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
                    if monitor['calibrated'] is not None and self.printer.status == PrinterStatus.PAUSED:
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
        self._is_stopped = True
        for task in self._background_task:
            task.cancel()
        await self.duet.disconnect()

    @Demands.WebcamTestEvent.on
    async def on_webcam_test(self, event: Demands.WebcamTestEvent) -> None:
        """Test the webcam."""
        self.printer.webcam_info.connected = (True if self.config.webcam_uri is not None else False)

    async def _send_webcam_snapshot(self, image) -> None:
        jpg_encoded = image
        base64_encoded = base64.b64encode(jpg_encoded).decode()
        await self.send_event(
            ClientEvents.StreamEvent(data={"base": base64_encoded}),
        )
        async with self._requested_webcam_snapshots_lock:
            self._requested_webcam_snapshots -= 1

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

                if self._requested_webcam_snapshots > 0 and self.intervals.is_ready(
                    IntervalTypes.WEBCAM,
                ):
                    await self._send_webcam_snapshot(image=image)
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
        if self._webcam_task_handle is None:
            self._webcam_task_handle = await self._webcam_task()

        async with self._requested_webcam_snapshots_lock:
            self._requested_webcam_snapshots += 1

    @Demands.StreamOffEvent.on
    async def on_stream_off(self, event: Demands.StreamOffEvent) -> None:
        """Turn off the webcam stream."""
        pass

    @Demands.HasGcodeChangesEvent.on
    async def on_has_gcode_changes(
        self,
        event: Demands.HasGcodeChangesEvent,
    ) -> None:
        """Check if there are GCode changes."""
        # print(event)
        pass

    @Demands.GetGcodeScriptBackupsEvent.on
    async def on_get_gcode_script_backups(
        self,
        event: Demands.GetGcodeScriptBackupsEvent,
    ) -> None:
        """Get GCode script backups."""
        # print(event)
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
