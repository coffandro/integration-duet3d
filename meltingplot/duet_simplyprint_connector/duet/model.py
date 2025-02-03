"""Duet Printer model class."""

import asyncio
import logging
from enum import auto

import aiohttp

from attr import define, field

from pyee.asyncio import AsyncIOEventEmitter

from strenum import CamelCaseStrEnum, StrEnum

from .api import RepRapFirmware


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


class DuetModelEvents(StrEnum):
    """Duet Model Events enum."""

    state = auto()
    objectmodel = auto()
    connect = auto()
    close = auto()


class DuetState(CamelCaseStrEnum):
    """Duet State enum."""

    disconnected = auto()
    starting = auto()
    updating = auto()
    off = auto()
    halted = auto()
    pausing = auto()
    paused = auto()
    resuming = auto()
    cancelling = auto()
    processing = auto()
    simulating = auto()
    busy = auto()
    changing_tool = auto()
    idle = auto()


@define
class DuetPrinter():
    """Duet Printer model class."""

    api = field(type=RepRapFirmware, factory=RepRapFirmware)
    om = field(type=dict, default=None)
    seqs = field(type=dict, factory=dict)
    logger = field(type=logging.Logger, factory=logging.getLogger)
    events = field(type=AsyncIOEventEmitter, factory=AsyncIOEventEmitter)
    sbc = field(type=bool, default=False)
    _reply = field(type=str, default=None)
    _wait_for_reply = field(type=asyncio.Event, factory=asyncio.Event)

    def __attrs_post_init__(self) -> None:
        """Post init."""
        self.api.callbacks[503] = self._http_503_callback
        self.events.on(DuetModelEvents.objectmodel, self._track_state)

    @property
    def state(self) -> DuetState:
        """Get the state of the printer."""
        try:
            return DuetState(self.om['state']['status'])
        except (KeyError, TypeError):
            return DuetState.disconnected

    async def _track_state(self, old_om: dict):
        """Track the state of the printer."""
        if old_om is None:
            return
        old_state = DuetState(old_om['state']['status'])
        if self.state != old_state:
            self.logger.debug(f"State change: {old_state} -> {self.state}")
            self.events.emit(DuetModelEvents.state, self.state)

    async def connect(self) -> None:
        """Connect the printer."""
        result = await self.api.connect()
        if 'isEmulated' in result:
            self.sbc = True
        result = await self._fetch_full_status()
        self.om = result['result']
        self.events.emit(DuetModelEvents.connect)

    async def close(self) -> None:
        """Close the printer."""
        await self.api.close()
        self.events.emit(DuetModelEvents.close)

    def connected(self) -> bool:
        """Check if the printer is connected."""
        if self.api.session is None:
            return False
        return True

    async def gcode(self, command: str, no_reply: bool = True) -> str:
        """Send a GCode command to the printer."""
        self.logger.debug(f"Sending GCode: {command}")
        self._wait_for_reply.clear()
        await self.api.rr_gcode(
            gcode=command,
            no_reply=True,
        )
        if no_reply:
            return ''
        return await self.reply()

    async def reply(self) -> str:
        """Get the last reply from the printer."""
        await self._wait_for_reply.wait()
        return self._reply

    async def _fetch_objectmodel_recursive(self, *args, **kwargs) -> dict:
        """
        Fetch the object model recursively.

        Duet2
        The implementation is recursive to fetch the object model in chunks.
        This is required because the object model is too large to fetch in a single request.
        The implementation might be slow because of the recursive nature of the function, but
        this helps to reduce the load on the duet board.

        Duet3 or SBC mode (isEmulated)
        The implementation is not recursive and fetches the object model in a single request
        starting from the second level of the object model (d=2).
        """
        depth = kwargs.get('depth', 1)

        if self.sbc and depth == 2:
            kwargs['depth'] = 99

        response = await self.api.rr_model(
            *args,
            **kwargs,
        )

        if (depth == 1 or self.sbc is False) and isinstance(response['result'], dict):
            for k, v in response['result'].items():
                sub_key = f"{k}" if kwargs['key'] == '' else f"{kwargs['key']}.{k}"
                sub_depth = (depth + 1) if isinstance(v, dict) else 99
                sub_kwargs = dict(kwargs)
                sub_kwargs['key'] = sub_key
                sub_kwargs['depth'] = sub_depth
                sub_response = await self._fetch_objectmodel_recursive(
                    *args,
                    **sub_kwargs,
                )
                response['result'][k] = sub_response['result']
        elif 'next' in response and response['next'] > 0:
            sub_kwargs = dict(kwargs)
            sub_kwargs['array'] = response['next']
            next_data = await self._fetch_objectmodel_recursive(
                *args,
                **sub_kwargs,
            )
            response['result'].extend(next_data['result'])
            response['next'] = 0

        return response

    async def _fetch_full_status(self) -> dict:
        try:
            response = await self._fetch_objectmodel_recursive(
                key='',
                depth=1,
                frequently=False,
                include_null=True,
                verbose=True,
            )
        except KeyError:
            response = {}

        return response

    async def _handle_om_changes(self, changes: dict):
        """Handle object model changes."""
        if 'reply' in changes:
            self._reply = await self.api.rr_reply()
            self._wait_for_reply.set()
            self.logger.debug(f"Reply: {self._reply}")
            changes.pop('reply')

        if 'volChanges' in changes:
            # TODO: handle volume changes
            changes.pop('volChanges')

        for key in changes:
            changed_obj = await self._fetch_objectmodel_recursive(
                key=key,
                depth=2,
                frequently=False,
                include_null=True,
                verbose=True,
            )
            self.om[key] = changed_obj['result']

    async def tick(self):
        """Tick the printer."""
        if not self.connected():
            await self.connect()
        if self.om is None:
            # fetch initial full object model
            result = await self._fetch_full_status()
            self.om = result['result']
            self.events.emit(DuetModelEvents.objectmodel, None)
        else:
            # fetch partial object model
            result = await self.api.rr_model(
                key='',
                depth=99,
                frequently=True,
                include_null=True,
                verbose=True,
            )
            # compare the dicts and return the difference
            changes = {}
            for key, value in result['result']['seqs'].items():
                if key not in self.seqs or self.seqs[key] != value:
                    changes[key] = value
            self.seqs = result['result']['seqs']
            old_om = dict(self.om)
            try:
                self.om = merge_dictionary(self.om, result['result'])
                if changes:
                    await self._handle_om_changes(changes)
                self.events.emit(DuetModelEvents.objectmodel, old_om)
            except (TypeError, KeyError):
                self.logger.debug("Failed to update object model")

    async def _http_503_callback(self, error: aiohttp.ClientResponseError):
        """503 callback."""
        # there are no more than 10 clients connected to the duet board
        for _ in range(10):
            reply = await self.api.rr_reply(nocache=True)
            if reply == '':
                break
            self._reply = reply
        self._wait_for_reply.set()
