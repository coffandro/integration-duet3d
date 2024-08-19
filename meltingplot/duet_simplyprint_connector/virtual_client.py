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


@dataclass
class VirtualConfig(PrinterConfig):
    """Configuration for the VirtualClient."""

    duet_uri: Optional[str] = None
    duet_password: Optional[str] = None
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

    def _file_progress(self, progress: float) -> None:
        self.printer.file_progress.percent = 50 + (max(0, min(50, progress / 2)))
        # Ensure we send events to SimplyPrint
        asyncio.run_coroutine_threadsafe(self.consume_state(), self.event_loop)

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
                response = await self.duet.rr_fileinfo(name="0:/gcodes/{!s}".format(event.file_name))
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
        file_task = asyncio.create_task(
            self._download_and_upload_file_task(event=event),
        )
        self._background_task.add(file_task)
        file_task.add_done_callback(self._background_task.discard)

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

    async def init(self) -> None:
        """Initialize the client."""
        self._printer_timeout = time.time() + 60 * 5  # 5 minutes
        printer_status_task = asyncio.create_task(self._printer_status_task())
        self._background_task.add(printer_status_task)
        printer_status_task.add_done_callback(self._background_task.discard)

    async def _connect_to_duet(self) -> None:
        try:
            response = await self.duet.connect()
            self.logger.debug("Response from Duet: {!s}".format(response))
        except asyncio.TimeoutError:
            self.printer.status = PrinterStatus.OFFLINE
        except aiohttp.ClientError as e:
            self.logger.debug(
                "Failed to connect to Duet with error: {!s}".format(e),
            )

        try:
            board = await self.duet.rr_model(key='boards[0]')
        except Exception as e:
            self.logger.error('Error connecting to Duet Board: {0}'.format(e))

        self.logger.info('Connected to Duet Board {0}'.format(board['result']))

        self.printer.firmware.name = board['result']['firmwareName']
        self.printer.firmware.version = board['result']['firmwareVersion']
        self.set_info("RepRapFirmware", "0.0.1")
        self._duet_connected = True

    async def _update_temperatures(self, printer_status: dict) -> None:
        self.printer.bed_temperature.actual = printer_status['result']['heat']['heaters'][0]['current']
        if printer_status['result']['heat']['heaters'][0]['state'] != 'off':
            self.printer.bed_temperature.target = printer_status['result']['heat']['heaters'][0]['active']
        else:
            self.printer.bed_temperature.target = 0.0

        self.printer.tool_temperatures[0].actual = printer_status['result']['heat']['heaters'][1]['current']

        if printer_status['result']['heat']['heaters'][1]['state'] != 'off':
            self.printer.tool_temperatures[0].target = printer_status['result']['heat']['heaters'][1]['active']
        else:
            self.printer.tool_temperatures[0].target = 0.0

        self.printer.ambient_temperature.ambient = 20

    async def _fetch_printer_status(self) -> dict:
        try:
            printer_status = await self.duet.rr_model(
                key='',
                frequently=True,
            )
        except TimeoutError:
            printer_status = None
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
        except TimeoutError:
            job_status = None
        except Exception:
            self.logger.exception(
                "An exception occurred while updating the job info",
            )
            # use old job status if new one is not available
            job_status = self._job_status
        return job_status

    async def _printer_status_task(self) -> None:
        while not self._is_stopped:
            try:
                if not self._duet_connected:
                    await self._connect_to_duet()
            except Exception:
                await asyncio.sleep(60)
                continue

            printer_status = await self._fetch_printer_status()

            async with self._printer_status_lock:
                self._printer_status = printer_status

            await asyncio.sleep(1)

            job_status = await self._fetch_job_status()

            async with self._job_status_lock:
                self._job_status = job_status

            await asyncio.sleep(1)

    async def _update_printer_state(self, printer_status: dict) -> None:
        try:
            printer_state = printer_status['result']['state']['status']
        except KeyError:
            printer_state = 'disconnected'

        # SP is sensitive to printer status while printing
        # so we need to differentiate between printer status while printing and not printing
        if await self._is_printing():
            self.printer.status = duet_state_simplyprint_status_while_printing_mapping[printer_state]
        else:
            self.printer.status = duet_state_simplyprint_status_mapping[printer_state]

    async def _update_printer_status(self) -> None:
        async with self._printer_status_lock:
            printer_status = self._printer_status

        if printer_status is None:
            if time.time() > self._printer_timeout:
                self.printer.status = PrinterStatus.OFFLINE
            return

        try:
            await self._update_temperatures(printer_status)
        except KeyError:
            self.printer.bed_temperature.actual = 0.0
            self.printer.tool_temperatures[0].actual = 0.0

        await self._update_printer_state(printer_status)

        if self.printer.status == PrinterStatus.CANCELLING and self.printer.job_info.started:
            self.printer.job_info.cancelled = True
        elif self.printer.status == PrinterStatus.OPERATIONAL:  # The machine is on but has nothing to do
            if self.printer.job_info.started:
                self.printer.job_info.finished = True

        self._printer_timeout = time.time() + 60 * 5  # 5 minutes

    async def _is_printing(self) -> bool:
        printing = (
            self.printer.status == PrinterStatus.PRINTING or self.printer.status == PrinterStatus.PAUSED
            or self.printer.status == PrinterStatus.PAUSING or self.printer.status == PrinterStatus.RESUMING
        )

        async with self._job_status_lock:
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
        async with self._job_status_lock:
            job_status = self._job_status

        if job_status is None:
            return

        job_status = job_status['result']

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

            await self._update_printer_status()

            if self.printer.status != PrinterStatus.OFFLINE:
                if self._requested_webcam_snapshots > 0 and self.intervals.is_ready(
                    IntervalTypes.WEBCAM,
                ):
                    await self._send_webcam_snapshot()

                if await self._is_printing():
                    await self._update_job_info()
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

    async def _send_webcam_snapshot(self) -> None:
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

    async def _webcam_task(self) -> None:
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
            self._webcam_task_handle = asyncio.create_task(self._webcam_task())

            def remove_task(task):
                self._webcam_task_handle = None

            self._webcam_task_handle.add_done_callback(remove_task)

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
