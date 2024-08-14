"""A virtual client for the SimplyPrint.io Service."""

import asyncio
import base64
import pathlib
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

import imageio.v3 as iio

from simplyprint_ws_client.client.client import DefaultClient
from simplyprint_ws_client.client.config import PrinterConfig
from simplyprint_ws_client.client.protocol import ClientEvents, Demands, Events
from simplyprint_ws_client.client.state.printer import FileProgressState, PrinterStatus
from simplyprint_ws_client.helpers.file_download import FileDownload
from simplyprint_ws_client.helpers.intervals import IntervalTypes

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


@dataclass
class VirtualConfig(PrinterConfig):
    """Configuration for the VirtualClient."""

    duet_uri: Optional[str] = None
    duet_password: Optional[str] = None
    webcam_uri: Optional[str] = None


class VirtualClient(DefaultClient[VirtualConfig]):
    """A Websocket client for the SimplyPrint.io Service."""

    def __init__(self, *args, **kwargs):
        """Initialize the client."""
        super().__init__(*args, **kwargs)

        self.duet = RepRapFirmware(
            address=self.config.duet_uri,
            password=self.config.duet_password,
            logger=self.logger,
        )

        self._duet_connected = False
        self._printer_status = None
        self._printer_status_lock = asyncio.Lock()
        self._job_status = None
        self._job_status_lock = asyncio.Lock()

        self._webcam_timeout = 0
        self._webcam_task_handle = None
        self._webcam_image = None
        self._webcam_image_lock = asyncio.Lock()
        self._requested_webcam_snapshots = 0
        self._requested_webcam_snapshots_lock = asyncio.Lock()

        self._background_task = set()
        self._is_stopped = False

    @Events.ConnectEvent.on
    async def on_connect(self, event: Events.ConnectEvent):
        """Connect to the printer."""
        self.logger.info('Connected to Simplyprint.io')

    @Events.PrinterSettingsEvent.on
    async def on_printer_settings(self, event: Events.PrinterSettingsEvent):
        """Update the printer settings."""
        self.logger.debug("Printer settings: %s", event.data)

    @Demands.GcodeEvent.on
    async def on_gcode(self, event: Demands.GcodeEvent):
        """Send GCode to the printer."""
        self.logger.debug("Gcode: {!r}".format(event.list))

        gcode = GCodeBlock().parse(event.list)
        self.logger.debug("Gcode: {!r}".format(gcode))

        allowed_commands = [
            'M112',
            'M104',
            'M140',
            'M106',
            'M107',
            'M221',
            'M220',
            'G91',
            'G1',
            'G90',
            'G28',
            'M18',
            'M17',
            'M190',
            'M109',
        ]

        response = []

        for item in gcode.code:
            if item.code in allowed_commands:
                response.append(await self.duet.rr_gcode(item.compress()))
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
        # M18
        # M17
        # M190
        # M109
        # M155 # not supported by reprapfirmware

    def _file_progress(self, progress):
        self.printer.file_progress.percent = 50 + int(
            max(0, min(50, progress / 2)),
        )
        # Ensure we send events to SimplyPrint
        asyncio.run_coroutine_threadsafe(self.consume_state(), self.event_loop)

    async def _download_and_upload_file(self, event: Demands.FileEvent):
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
                    await self.duet.rr_upload_stream(
                        filepath='{!s}{!s}'.format(prefix, event.file_name),
                        file=f,
                        progress=self._file_progress,
                    )
                    break
                except aiohttp.ClientResponseError as e:
                    if e.status == 401:
                        await self.duet.reconnect()
                finally:
                    retries -= 1

        self.printer.file_progress.percent = 100.0
        self.printer.file_progress.state = FileProgressState.READY
        if event.auto_start:
            self.printer.job_info.filename = event.file_name
            timeout = time.time() + 60 * 5  # 5 minutes
            while timeout > time.time():
                response = await self.duet.rr_fileinfo(
                    name="0:/gcodes/{!s}".format(event.file_name),
                )
                if response['err'] == 0:
                    break
                # Ensure we send events to SimplyPrint
                asyncio.run_coroutine_threadsafe(
                    self.consume_state(),
                    self.event_loop,
                )
                await asyncio.sleep(1)

            asyncio.run_coroutine_threadsafe(
                self.on_start_print(event),
                self.event_loop,
            )

    async def _download_and_upload_file_task(self, event: Demands.FileEvent):
        try:
            await self._download_and_upload_file(event)
        except Exception as e:
            self.logger.exception(
                "An exception occurred while downloading and uploading a file",
                exc_info=e,
            )

    @Demands.FileEvent.on
    async def on_file(self, event: Demands.FileEvent):
        """Download a file from Simplyprint.io to the printer."""
        file_task = asyncio.create_task(
            self._download_and_upload_file_task(event=event),
        )
        self._background_task.add(file_task)
        file_task.add_done_callback(self._background_task.discard)

    @Demands.StartPrintEvent.on
    async def on_start_print(self, _):
        """Start the print job."""
        await self.duet.rr_gcode(
            'M23 "0:/gcodes/{!s}"'.format(self.printer.job_info.filename),
        )
        await self.duet.rr_gcode('M24')

    @Demands.PauseEvent.on
    async def on_pause_event(self, _):
        """Pause the print job."""
        await self.duet.rr_gcode('M25')

    @Demands.ResumeEvent.on
    async def on_resume_event(self, _):
        """Resume the print job."""
        await self.duet.rr_gcode('M24')

    @Demands.CancelEvent.on
    async def on_cancel_event(self, _):
        """Cancel the print job."""
        await self.duet.rr_gcode('M25')
        await self.duet.rr_gcode('M0')

    async def init(self):
        """Initialize the client."""
        self.printer.status = PrinterStatus.OFFLINE

        printer_status_task = asyncio.create_task(self._printer_status_task())
        self._background_task.add(printer_status_task)
        printer_status_task.add_done_callback(self._background_task.discard)

    async def _connect_to_duet(self):
        try:
            response = await self.duet.connect()
            self.logger.debug("Response from Duet: {!s}".format(response))
        except (asyncio.TimeoutError, aiohttp.ClientError):
            self.printer.status = PrinterStatus.OFFLINE

        try:
            board = await self.duet.rr_model(key='boards[0]')
        except Exception as e:
            self.logger.error('Error connecting to Duet Board: {0}'.format(e))

        self.logger.info('Connected to Duet Board {0}'.format(board['result']))

        self.printer.firmware.name = board['result']['firmwareName']
        self.printer.firmware.version = board['result']['firmwareVersion']
        self.set_info("RepRapFirmware", "0.0.1")
        self._duet_connected = True

    async def _update_temperatures(self, printer_status):
        self.printer.bed_temperature.actual = printer_status['result']['heat'][
            'heaters'][0]['current']
        if printer_status['result']['heat']['heaters'][0]['state'] != 'off':
            self.printer.bed_temperature.target = printer_status['result'][
                'heat']['heaters'][0]['active']
        else:
            self.printer.bed_temperature.target = 0.0

        self.printer.tool_temperatures[0].actual = printer_status['result'][
            'heat']['heaters'][1]['current']

        if printer_status['result']['heat']['heaters'][1]['state'] != 'off':
            self.printer.tool_temperatures[0].target = printer_status[
                'result']['heat']['heaters'][1]['active']
        else:
            self.printer.tool_temperatures[0].target = 0.0

        self.printer.ambient_temperature.ambient = 20

    async def _printer_status_task(self):
        while not self._is_stopped:
            try:
                if not self._duet_connected:
                    await self._connect_to_duet()
            except Exception:
                await asyncio.sleep(60)
                continue

            try:
                printer_status = await self.duet.rr_model(
                    key='',
                    frequently=True,
                )
            except Exception:
                self.logger.exception(
                    "An exception occurred while updating the printer status",
                )
                printer_status = None
            async with self._printer_status_lock:
                self._printer_status = printer_status

            await asyncio.sleep(1)

            try:
                job_status = await self.duet.rr_model(
                    key='job',
                    frequently=False,
                    depth=5,
                )
            except Exception:
                self.logger.exception(
                    "An exception occurred while updating the job info",
                )
                job_status = None

            async with self._job_status_lock:
                self._job_status = job_status

            await asyncio.sleep(1)

    async def _update_printer_status(self):
        async with self._printer_status_lock:
            printer_status = self._printer_status

        if printer_status is None:
            self.printer.status = PrinterStatus.OFFLINE
            return

        try:
            await self._update_temperatures(printer_status)
        except KeyError:
            self.printer.bed_temperature.actual = 0.0
            self.printer.tool_temperatures[0].actual = 0.0

        try:
            printer_state = printer_status['result']['state']['status']
        except KeyError:
            printer_state = 'disconnected'

        # disconnected: Not connected to the Duet
        # starting: Processing config.g
        # updating: The firmware is being updated
        # off: The machine is turned off (i.e. the input voltage is too low for operation)
        # halted: The machine has encountered an emergency stop and is ready to reset
        # pausing: The machine is about to pause a file job
        # paused: The machine has paused a file job
        # resuming: The machine is about to resume a paused file job
        # cancelling: Job file is being cancelled
        # processing: The machine is processing a file job
        # simulating: The machine is simulating a file job to determine its processing time
        # busy: The machine is busy doing something (e.g. moving)
        # changingTool: The machine is changing the current tool
        # idle: The machine is on but has nothing to do

        self.printer.status = duet_state_simplyprint_status_mapping[
            printer_state]
        if self.printer.status == PrinterStatus.CANCELLING and self.printer.job_info.started:
            self.printer.job_info.cancelled = True
        elif self.printer.status == PrinterStatus.OPERATIONAL:  # The machine is on but has nothing to do
            if self.printer.job_info.started:
                self.printer.job_info.finished = True
            self.printer.job_info.started = False

    def _is_printing(self):
        return (
            self.printer.status == PrinterStatus.PRINTING
            or self.printer.status == PrinterStatus.PAUSED
            or self.printer.status == PrinterStatus.PAUSING
            or self.printer.status == PrinterStatus.RESUMING
        )

    async def _update_job_info(self):
        async with self._job_status_lock:
            job_status = self._job_status

        if job_status is None:
            return

        try:
            total_filament_required = sum(
                job_status['result']['file']['filament'],
            )
            current_filament = float(job_status['result']['rawExtrusion'])
            self.printer.job_info.progress = min(
                current_filament * 100.0 / total_filament_required,
                100.0,
            )
            self.printer.job_info.filament = round(current_filament, 0)
        except (TypeError, KeyError):
            self.printer.job_info.progress = 0.0

        try:
            self.printer.job_info.time = job_status['result']['timesLeft'][
                'filament']
        except (TypeError, KeyError):
            self.printer.job_info.time = 0

        try:
            filepath = job_status['result']['file']['fileName']
            self.printer.job_info.filename = pathlib.PurePath(
                filepath,
            ).name
            self.printer.job_info.started = True
        except (TypeError, KeyError):
            self.printer.job_info.filename = None

        self.printer.job_info.layer = job_status['result'][
            'layer'] if 'layer' in job_status['result'] else 0

    async def tick(self):
        """Update the client state."""
        try:
            await self.send_ping()

            await self._update_printer_status()

            if self.printer.status != PrinterStatus.OFFLINE:
                if self._requested_webcam_snapshots > 0 and self.intervals.is_ready(
                    IntervalTypes.WEBCAM,
                ):
                    await self._send_webcam_snapshot()

                if self._is_printing():
                    await self._update_job_info()
        except Exception as e:
            self.logger.exception(
                "An exception occurred while ticking the client state",
                exc_info=e,
            )

    async def stop(self):
        """Stop the client."""
        self._is_stopped = True
        for task in self._background_task:
            task.cancel()
        await self.duet.disconnect()

    @Demands.WebcamTestEvent.on
    async def on_webcam_test(self, event: Demands.WebcamTestEvent):
        """Test the webcam."""
        self.printer.webcam_info.connected = (
            True if self.config.webcam_uri is not None else False
        )

    async def _send_webcam_snapshot(self):
        async with self._webcam_image_lock:
            if self._webcam_image is None:
                return

            jpg_encoded = self._webcam_image
        base64_encoded = base64.b64encode(jpg_encoded).decode()
        await self.send_event(
            ClientEvents.StreamEvent(data={"base": base64_encoded}),
        )
        async with self._requested_webcam_snapshots_lock:
            self._requested_webcam_snapshots -= 1

    async def _fetch_webcam_image(self):
        headers = {"Accept": "image/jpeg"}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
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

    async def _webcam_task(self):
        self.logger.debug('Webcam task started')
        while time.time() < self._webcam_timeout:
            try:
                image = await self._fetch_webcam_image()
                async with self._webcam_image_lock:
                    self._webcam_image = image
            except Exception as e:
                self.logger.debug("Failed to fetch webcam image: {}".format(e))
            await asyncio.sleep(10)
        async with self._webcam_image_lock:
            self._webcam_image = None

    @Demands.WebcamSnapshotEvent.on
    async def on_webcam_snapshot(self, event: Demands.WebcamSnapshotEvent):
        """Take a snapshot from the webcam."""
        self._webcam_timeout = time.time() + 60
        if self._webcam_task_handle is None:
            self._webcam_task_handle = asyncio.create_task(self._webcam_task())

            def remove_task(task):
                self._webcam_task_handle = None

            self._webcam_task_handle.add_done_callback(remove_task)

        async with self._requested_webcam_snapshots_lock:
            self._requested_webcam_snapshots += 1

    @Demands.StreamOffEvent.on
    async def on_stream_off(self, event: Demands.StreamOffEvent):
        """Turn off the webcam stream."""
        pass

    @Demands.HasGcodeChangesEvent.on
    async def on_has_gcode_changes(self, event: Demands.HasGcodeChangesEvent):
        """Check if there are GCode changes."""
        # print(event)
        pass

    @Demands.GetGcodeScriptBackupsEvent.on
    async def on_get_gcode_script_backups(
        self,
        event: Demands.GetGcodeScriptBackupsEvent,
    ):
        """Get GCode script backups."""
        # print(event)
        pass

    @Demands.ApiRestartEvent.on
    async def on_api_restart(self, event: Demands.ApiRestartEvent):
        """Restart the API."""
        self.logger.info("Restarting API")
        # the api is running as a systemd service, so we can just restart the service
        # by terminating the process
        raise KeyboardInterrupt()
