"""Duet Printer model class."""

import asyncio
import logging

import aiohttp

from attr import define, field

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


@define
class DuetPrinter():
    """Duet Printer model class."""

    api = field(type=RepRapFirmware, factory=RepRapFirmware)
    om = field(type=dict, default=None)
    seqs = field(type=dict, default=None)
    logger = field(type=logging.Logger, factory=logging.getLogger)
    _reply = field(type=str, default=None)
    _wait_for_reply = field(type=asyncio.Event, factory=asyncio.Event)

    def __attrs_post_init__(self):
        """Post init."""
        self.api.callbacks[503] = self.http_503_callback

    async def close(self):
        """Close the printer."""
        await self.api.close()

    async def _fetch_partial_status(self, *args, **kwargs) -> dict:
        response = await self.api.rr_model(
            *args,
            **kwargs,
        )

        # TODO: remove when duet fixed this
        # internal duet server is buffering the replay for every connected client
        # so we need to fetch the response to free the buffer even if we don't need the response
        # await self.duet.rr_reply()
        return response

    async def _fetch_objectmodel_recursive(self, *args, **kwargs) -> dict:
        """
        Fetch the object model recursively.

        The implementation is recursive to fetch the object model in chunks.
        This is required because the object model is too large to fetch in a single request.
        The implementation might be slow because of the recursive nature of the function, but
        this helps to reduce the load on the duet board.
        """
        depth = kwargs.get('depth', 1)

        response = await self.api.rr_model(
            *args,
            **kwargs,
        )

        if isinstance(response['result'], dict):
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
        response = await self._fetch_objectmodel_recursive(
            key='',
            depth=1,
            frequently=False,
            include_null=True,
            verbose=True,
        )

        return response

    async def gcode(self, command: str) -> str:
        """Send a GCode command to the printer."""
        self._wait_for_reply.clear()
        await self.api.rr_gcode(
            command=command,
            no_reply=True,
        )
        await self._wait_for_reply.wait()
        return self._reply

    async def reply(self) -> str:
        """Get the last reply from the printer."""
        await self._wait_for_reply.wait()
        await asyncio.sleep(0)  # Allow other tasks to process the event
        return self._reply

    async def _handle_om_changes(self, changes: dict):
        """Handle object model changes."""
        if 'reply' in changes:
            self._reply = await self.api.rr_reply()
            self._wait_for_reply.set()
            await asyncio.sleep(0)  # Allow other tasks to process the event
            self._wait_for_reply.clear()
        if 'volChanges' in changes:
            # TODO: handle volume changes
            changes.pop('volChanges')
        for key in changes:
            if key == 'reply':
                continue
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
        if self.om is None:
            # fetch initial full object model
            result = await self._fetch_full_status()
            self.om = result['result']
        else:
            # fetch partial object model
            result = await self.api.rr_model(key='seqs')
            # compare the dicts and return the difference
            changes = {}
            if self.seqs is None:
                changes = result['result']
            elif self.seqs is not None:
                for key, value in result['result'].items():
                    if key in self.seqs and self.seqs[key] != value:
                        changes[key] = value

            self.seqs = result['result']

            if not changes:
                return

            await self._handle_om_changes(changes)

    async def http_503_callback(self, error: aiohttp.ClientResponseError):
        """503 callback."""
        # there are no more than 10 clients connected to the duet board
        for _ in range(10):
            reply = await self.api.rr_reply(nocache=True)
            if reply == '':
                break
            self._reply = reply
        self._wait_for_reply.set()
        await asyncio.sleep(0)  # Allow other tasks to process the event
        self._wait_for_reply.clear()


async def printer_task(printer):
    """Printer task."""
    while True:
        await printer.tick()
        await asyncio.sleep(0.25)


async def main():
    """Execute the main function."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    api = RepRapFirmware(
        address="http://192.168.172.75",
        password="meltingplot",
        logger=logging.getLogger(f"{__name__}.RepRapFirmware"),
    )
    api.logger.setLevel(logging.INFO)

    printer = DuetPrinter(api=api)

    asyncio.create_task(printer_task(printer))

    try:
        while True:
            reply = await printer.reply()
            reply = reply.replace('\n', '\\n')
            logging.getLogger().debug(f"Reply: {reply}")
            await asyncio.sleep(0.25)
    except KeyboardInterrupt:
        pass

    await asyncio.run(printer.close())


if __name__ == "__main__":
    asyncio.run(main())
