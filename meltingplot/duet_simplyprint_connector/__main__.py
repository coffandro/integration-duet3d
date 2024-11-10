"""Main entry point for the Duet SimplyPrint connector."""
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import click

from simplyprint_ws_client.cli import ClientCli
from simplyprint_ws_client.client import ClientApp, ClientMode, ClientOptions
from simplyprint_ws_client.client.config import ConfigManagerType
from simplyprint_ws_client.client.logging import ClientHandler
from simplyprint_ws_client.helpers.url_builder import SimplyPrintBackend

from . import __version__
from .cli.autodiscover import AutoDiscover
from .cli.install import install_as_service
from .virtual_client import VirtualClient, VirtualConfig


def rescan_existing_networks(app):
    """
    Rescan the existing networks.

    Gather all the existing networks and password from the configuration
    manager and scan them.
    """
    configs = app.config_manager.get_all()
    networks = {}
    for config in configs:
        try:
            # Attempt to resolve the URI as a URL via DNS
            hostname = urlparse(config.duet_uri).hostname  # Extract hostname from URI
            ip_address = socket.gethostbyname(hostname)
            network = ipaddress.ip_network(ip_address, strict=False).supernet(new_prefix=24)
        except (socket.gaierror, ValueError, TypeError):
            # If DNS resolution fails, treat it as an IP address directly
            network = ipaddress.ip_network(config.duet_uri, strict=False).supernet(new_prefix=24)
        networks[f"{network}"] = config.duet_password
    return networks


def run_app(autodiscover, app):
    """Run the application."""
    click.echo("Starting the Meltingplot Duet SimplyPrint.io Connector")
    click.echo('Perform network scans for existing networks')

    networks = rescan_existing_networks(app)

    for network, pwd in networks.items():
        click.echo(f"Scanning existing network: {network} with password {pwd}")
        autodiscover._autodiscover(password=pwd, ipv4_range=network, ipv6_range="::1/128")

    app.run_blocking()


def main():
    """Initiate the connector as the main entry point."""
    client_options = ClientOptions(
        name="DuetConnector",
        version=__version__,
        mode=ClientMode.MULTI_PRINTER,
        client_t=VirtualClient,
        config_t=VirtualConfig,
        allow_setup=True,
        config_manager_type=ConfigManagerType.JSON,
        backend=SimplyPrintBackend.PRODUCTION,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s.%(funcName)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            ClientHandler.root_handler(client_options),
        ],
    )

    app = ClientApp(client_options)
    cli = ClientCli(app)

    autodiscover = AutoDiscover(app)

    cli.add_command(autodiscover.autodiscover)
    cli.add_command(install_as_service)
    cli.add_command(click.Command("start", callback=lambda: run_app(autodiscover, app), help="Start the client"))
    cli(prog_name="python -m meltingplot.duet_simplyprint_connector")


if __name__ == "__main__":
    main()
