"""
This module provides functionality to autodiscover devices within specified IP ranges.

The module connects to them using the RepRapFirmware API.
It uses the RepRapFirmware API to connect to devices and retrieve their unique IDs.

Functions:
    convert_cidr_to_list(ip_range) -> list:
        Convert a CIDR notation IP range to a list of individual IP addresses.

    connect_to_duet(ip_address, password):
        Connect to a printer using the specified IP address and password.

    connect_to_range(password, ipv4_range, ipv6_range):
        Connect to all devices within the specified IP ranges.

    autodiscover(password, ipv4_range, ipv6_range):
        Autodiscover devices in the specified IP range using the provided password.
"""
import asyncio
import ipaddress

import aiohttp

import click

from ..duet.api import RepRapFirmware


def convert_cidr_to_list(ip_range) -> list:
    """Convert a CIDR notation IP range to a list of individual IP addresses."""
    try:
        ip_network = ipaddress.ip_network(ip_range, strict=False)
        return list(ip_network.hosts())
    except ValueError:
        return None


async def connect_to_duet(ip_address, password):
    """Connect to a printer using the specified IP address and password."""
    duet = RepRapFirmware(
        address=ip_address,
        password=password,
    )

    try:
        await duet.connect()
    except (
        aiohttp.client_exceptions.ClientConnectorError,
        aiohttp.ClientError,
        asyncio.exceptions.CancelledError,
        asyncio.exceptions.TimeoutError,
        OSError,
    ):
        await duet.close()
        return None

    try:
        board = await duet.rr_model(key='boards[0]')
        board = board['result']
    except (
        aiohttp.ClientError,
        TimeoutError,
        asyncio.exceptions.CancelledError,
        asyncio.exceptions.TimeoutError,
    ):
        return None
    finally:
        await duet.close()

    board['uniqueId']

    return {
        'duet_uri': f'{ip_address}',
        'duet_password': password,
        'duet_unique_id': f"{board['uniqueId']}",
    }


async def connect_to_range(password, ipv4_range, ipv6_range):
    """Connect to all devices within the specified IP ranges."""
    tasks = []
    for ipv4 in ipv4_range:
        tasks.append(connect_to_duet(ipv4, password))
    for ipv6 in ipv6_range:
        tasks.append(connect_to_duet(f"[{ipv6}]", password))

    return await asyncio.gather(*tasks)


@click.command()
@click.option(
    '--password',
    prompt=True,
    default='reprap',
    hide_input=False,
    confirmation_prompt=False,
    help='Password for authentication',
)
@click.option('--ipv4-range', prompt=True, default='192.168.1.0/24', help='IP range to scan for devices')
@click.option('--ipv6-range', prompt=True, default='::1/128', help='IPv6 range to scan for devices')
def autodiscover(password, ipv4_range, ipv6_range):
    """Autodiscover devices in the specified IP range."""
    ipv4_addresses = convert_cidr_to_list(ipv4_range)
    ipv6_addresses = convert_cidr_to_list(ipv6_range)

    click.echo(
        f'Starting autodiscovery with password: {password}, '
        f'IPv4 range: {ipv4_range}, and IPv6 range: {ipv6_range}',
    )

    responses = asyncio.run(connect_to_range(password, ipv4_addresses, ipv6_addresses))

    for client in responses:
        if client is not None:
            click.echo(f'{client}')
