#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Duet Web Control HTTP Api Module."""

import asyncio
import datetime
import logging
from typing import AsyncIterable, BinaryIO, Callable, Optional
from zlib import crc32

import aiohttp

import attr


def reauthenticate(retries=3):
    """Reauthenticate HTTP API requests."""

    def decorator(f):

        async def inner(*args, **kwargs):
            status = {'retries': retries}
            while status['retries']:
                try:
                    return await f(*args, **kwargs)
                except TimeoutError:
                    args[0].logger.error('TimeoutError - retry')
                    status['retries'] -= 1
                    await asyncio.sleep(5**(retries - status['retries']))
                except aiohttp.ClientResponseError as e:
                    status_code = e.status
                    status['retries'] -= 1
                    if status_code == 401:
                        args[0].logger.error(
                            'Unauthorized  while requesting {!s} - retry'.format(e.request_info),
                        )
                        await asyncio.sleep(5**(retries - status['retries']))
                        response = await args[0].reconnect()
                        if response['err'] == 0:
                            status['retries'] = retries
                    elif status_code == 503:
                        # Besides, RepRapFirmware may run short on memory and
                        # may not be able to respond properly. In this case,
                        # HTTP status code 503 is returned.
                        args[0].logger.error('Duet busy - retry')
                        await asyncio.sleep(60)
                    else:
                        raise e
            raise TimeoutError(
                'Retried {} times to reauthenticate.'.format(retries),
            )

        return inner

    return decorator


@attr.s
class RepRapFirmware():
    """RepRapFirmware API Class."""

    address = attr.ib(type=str, default="10.42.0.2")
    password = attr.ib(type=str, default="meltingplot")
    session_timeout = attr.ib(type=int, default=8000)
    http_timeout = attr.ib(type=int, default=15)
    http_retries = attr.ib(type=int, default=3)
    session = attr.ib(type=aiohttp.ClientSession, default=None)
    logger = attr.ib(type=logging.Logger, factory=logging.getLogger)

    async def connect(self) -> dict:
        """Connect to the Duet."""
        return await self.reconnect()

    async def reconnect(self) -> dict:
        """Reconnect to the Duet."""
        url = 'http://{0}/rr_connect'.format(self.address)

        params = {
            'password': self.password,
            'sessionKey': 'yes',
        }

        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.http_timeout),
                raise_for_status=True,
            )
        else:
            self.session.headers.clear()

        json_response = {}
        async with self.session.get(url, params=params) as r:
            json_response = await r.json()

        try:
            if json_response['err'] == 0:
                if 'sessionKey' in json_response:
                    self.session.headers['X-Session-Key'] = '{!s}'.format(
                        json_response['sessionKey'],
                    )
                if 'sessionTimeout' in json_response:
                    self.session_timeout = json_response['sessionTimeout']
        except KeyError as e:
            raise e

        return json_response

    async def disconnect(self) -> dict:
        """Disconnect from the Duet."""
        url = 'http://{0}/rr_disconnect'.format(self.address)

        response = {}
        async with self.session.get(url) as r:
            response = await r.json()
        await self.session.close()
        return response

    @reauthenticate()
    async def rr_model(
        self,
        key: Optional[str] = None,
        frequently: Optional[bool] = False,
        verbose: Optional[bool] = False,
        include_null: Optional[bool] = False,
        include_obsolete: Optional[bool] = False,
        depth: Optional[int] = 99,
    ) -> dict:
        """rr_model Get Machine Model."""
        url = 'http://{0}/rr_model'.format(self.address)

        flags = []

        if frequently:
            flags.append('f')

        if verbose:
            flags.append('v')

        if include_null:
            flags.append('n')

        if include_obsolete:
            flags.append('o')

        flags.append('d{:d}'.format(depth))

        params = {
            'key': key if key is not None else '',
            'flags': ''.join(flags),
        }

        response = {}
        async with self.session.get(url, params=params) as r:
            response = await r.json()
        return response

    @reauthenticate()
    async def rr_gcode(self, gcode: str) -> str:
        """rr_gcode Send GCode to Duet."""
        url = 'http://{0}/rr_gcode'.format(self.address)

        params = {
            'gcode': gcode,
        }

        async with self.session.get(url, params=params):
            pass

        return await self.rr_reply()

    @reauthenticate()
    async def rr_reply(self) -> str:
        """rr_reply Get Reply from Duet."""
        url = 'http://{0}/rr_reply'.format(self.address)

        response = ''
        async with self.session.get(url) as r:
            response = await r.text()
        return response

    async def rr_download(
        self,
        filepath: str,
        chunk_size: Optional[int] = 1024,
    ) -> AsyncIterable:
        """rr_download Download File from Duet."""
        url = 'http://{0}/rr_download'.format(self.address)

        params = {
            'name': filepath,
        }

        async with self.session.get(url, params=params) as r:
            async for chunk in r.content.iter_chunked(chunk_size):
                yield chunk

    @reauthenticate()
    async def rr_upload(
        self,
        filepath: str,
        content: bytes,
        last_modified: Optional[datetime.datetime] = None,
    ) -> object:
        """rr_upload Upload File to Duet."""
        url = 'http://{0}/rr_upload'.format(self.address)

        params = {
            'name': filepath,
        }

        if last_modified is not None:
            params['time'] = last_modified.isoformat(timespec='seconds')

        try:
            checksum = crc32(content) & 0xffffffff
        except TypeError:
            content = content.encode('utf-8')
            checksum = crc32(content) & 0xffffffff

        params['crc32'] = '{0:08x}'.format(checksum)

        response = b''
        async with self.session.post(url, data=content, params=params) as r:
            response = await r.json()
        return response

    async def rr_upload_stream(
        self,
        filepath: str,
        file: BinaryIO,
        last_modified: Optional[datetime.datetime] = None,
        progress: Optional[Callable] = None,
    ) -> object:
        """rr_upload_stream Upload File to Duet."""
        url = 'http://{0}/rr_upload'.format(self.address)

        params = {
            'name': filepath,
        }

        if last_modified is not None:
            params['time'] = last_modified.isoformat(timespec='seconds')

        checksum = 0
        while chunk := file.read(8096):
            checksum = crc32(chunk, checksum) & 0xffffffff
            if progress:
                progress(0.0)

        checksum = checksum & 0xffffffff
        filesize = file.tell()
        file.seek(0)

        params['crc32'] = '{0:08x}'.format(checksum)

        async def file_chunk():
            while chunk := file.read(8096):
                if progress:
                    progress(
                        max(0.0, min(100.0,
                                     file.tell() / filesize * 100.0)),
                    )
                if not chunk:
                    break
                yield chunk

        timeout = aiohttp.ClientTimeout(
            total=60 * 30,  # 30 minutes
        )

        response = b''
        async with self.session.post(
            url=url,
            data=file_chunk(),
            params=params,
            timeout=timeout,
        ) as r:
            response = await r.json()

        return response

    @reauthenticate()
    async def rr_filelist(self, directory: str, **kwargs: any) -> object:
        """
        rr_filelist List Files in Directory.

        List Files in a Directory on the Duet.

        :param directory: Directory Path
        :type directory: str
        :param kwargs: Additional Parameters
        :type kwargs: any
        :return: File List
        :rtype: object
        """
        url = 'http://{0}/rr_filelist'.format(self.address)

        params = {
            'dir': directory,
        }

        response = {}
        async with self.session.get(url, params=params, **kwargs) as r:
            response = await r.json()

        return response

    @reauthenticate()
    async def rr_fileinfo(self, name: Optional[str] = None, **kwargs: any) -> object:
        """
        rr_fileinfo Get File Information.

        Get Information about a File on the Duet.

        :param name: Filepath
        :type name: str
        :param kwargs: Additional Parameters
        :type kwargs: any
        :return: File Information
        :rtype: object
        """
        url = 'http://{0}/rr_fileinfo'.format(self.address)

        params = {}

        if name is not None:
            params['name'] = name

        response = {}
        async with self.session.get(url, params=params, **kwargs) as r:
            response = await r.json()

        return response

    @reauthenticate()
    async def rr_mkdir(self, directory: str) -> object:
        """rr_mkdir Create a Folder.

        Create a Folder on the Duet.

        :param directory: Folder Path
        :type directory: str
        :return: Error Object
        :rtype: object
        """
        url = 'http://{0}/rr_mkdir'.format(self.address)

        params = {
            'dir': directory,
        }

        response = {}
        async with self.session.get(url, params=params) as r:
            response = await r.json()

        return response

    @reauthenticate()
    async def rr_move(
        self,
        old_filepath: str,
        new_filepath: str,
        overwrite: Optional[bool] = False,
    ) -> object:
        """rr_move Move File.

        Move a File on Filesystem.

        :param old_filepath: Source Filepath
        :type old_filepath: str
        :param new_filepath: Destination Filepath
        :type new_filepath: str
        :param overwrite: Override existing Destination, defaults to False
        :type overwrite: bool, optional
        :return: Error Object
        :rtype: object
        """
        url = 'http://{0}/rr_move'.format(self.address)

        params = {
            'old': old_filepath,
            'new': new_filepath,
            'deleteexisting': 'yes' if overwrite is True else 'no',
        }

        response = {}
        async with self.session.get(url, params=params) as r:
            response = await r.json()

        return response

    @reauthenticate()
    async def rr_delete(self, filepath: str) -> object:
        """rr_delete delete remote file.

        Delete File on Duet.

        :param filepath: Filepath
        :type filepath: str
        :return: Error Object
        :rtype: object
        """
        url = 'http://{0}/rr_delete'.format(self.address)

        params = {
            'name': filepath,
        }

        response = {}
        async with self.session.get(url, params=params) as r:
            response = await r.json()

        return response
