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

from simplyprint_ws_client.client import ClientApp

from ..duet.api import RepRapFirmware
from ..network import get_local_ip_and_mac


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
        board = await duet.rr_model(key='boards[0]')
        board = board['result']
        duet_name = await duet.rr_model(key='network.name')
        duet_name = duet_name['result']
    except (
        aiohttp.client_exceptions.ClientConnectorError,
        aiohttp.ClientError,
        asyncio.exceptions.CancelledError,
        asyncio.exceptions.TimeoutError,
        OSError,
        KeyError,
    ):
        return None
    finally:
        await duet.close()

    return {
        'duet_name': f"{duet_name}",
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


class AutoDiscover():
    """
    A class to handle the autodiscovery of devices within specified IP ranges.

    Attributes:
        app (ClientApp): The application instance.
        autodiscover (click.Command): The Click command for autodiscovery.

    Methods:
        __init__(app: ClientApp) -> None:
            Initializes the AutoDiscover class with the given application instance.
    """

    def __init__(self, app: ClientApp) -> None:
        """Initialize the AutoDiscover class with the given application instance."""
        self.app = app

        netinfo = get_local_ip_and_mac()
        ipv4_range = ipaddress.ip_network(netinfo.ip).supernet(new_prefix=24)
        default_ipv4_range = f"{ipv4_range}"

        self._autodiscover = self.autodiscover
        self.autodiscover = click.Command(
            name='autodiscover',
            callback=self.autodiscover,
            params=[
                click.Option(
                    ['--password'],
                    prompt=True,
                    default='reprap',
                    hide_input=False,
                    confirmation_prompt=False,
                    help='Password for authentication',
                ),
                click.Option(
                    ['--ipv4-range'],
                    prompt=True,
                    default=default_ipv4_range,
                    help='IPv4 range to scan for devices',
                ),
                click.Option(
                    ['--ipv6-range'],
                    prompt=True,
                    default='::1/128',
                    help='IPv6 range to scan for devices',
                ),
            ],
        )

    def autodiscover(self, password, ipv4_range, ipv6_range):
        """Autodiscover devices in the specified IP range."""
        ipv4_addresses = convert_cidr_to_list(ipv4_range)
        ipv6_addresses = convert_cidr_to_list(ipv6_range)

        click.echo(
            f'Starting autodiscovery with password: {password}, '
            f'IPv4 range: {ipv4_range}, and IPv6 range: {ipv6_range}',
        )

        responses = asyncio.run(connect_to_range(password, ipv4_addresses, ipv6_addresses))

        clients = {f"{client['duet_unique_id']}": client for client in responses if client is not None}

        self.app.logger.info(f'Found {len(clients)} devices.')

        for client in clients.values():
            self.app.logger.info(f'Found device: {client["duet_name"]}')

        configs = self.app.config_manager.get_all()
        for config in configs:
            if config.duet_unique_id in clients:
                self.app.logger.info(f'Found existing config for {config.duet_unique_id}. Updating.')

                config.duet_uri = clients[config.duet_unique_id]['duet_uri']
                clients.pop(config.duet_unique_id, None)

        for client in clients.values():
            self.app.logger.info(f'Adding new config for {client["duet_name"]} - {client["duet_unique_id"]}')
            config = self.app.config_manager.config_t.get_new()
            config.duet_name = client['duet_name']
            config.duet_uri = client['duet_uri']
            config.duet_password = client['duet_password']
            config.duet_unique_id = client['duet_unique_id']
            config.in_setup = True
            self.app.config_manager.persist(config)

        self.app.config_manager.flush()
