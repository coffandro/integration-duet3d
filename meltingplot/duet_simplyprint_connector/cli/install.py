"""Installscript of the Duet Simplyprint Connector."""


def install_as_service():
    """Install the Simplyprint Connector as a systemd service."""
    import os
    import getpass
    import grp
    import subprocess
    import sys
    import tempfile

    # Get the path of the service file
    service_file = os.path.join(sys.prefix, 'simplyprint-connector.service')

    service_content = None

    # Read the content of the service file
    with open(service_file, 'r') as file:
        service_content = file.read()

    # Replace User and Group with the current user and group
    current_user = getpass.getuser()
    current_group = grp.getgrgid(os.getgid()).gr_name
    service_content = service_content.replace('User=ubuntu', f'User={current_user}', count=1)
    service_content = service_content.replace('Group=ubuntu', f'Group={current_group}', count=1)

    # Save the modified content to a temp file
    with tempfile.NamedTemporaryFile('wt+') as tmp_file:
        file.write(service_content)

        # Copy the service file to /etc/systemd/system
        subprocess.run(['sudo', 'cp', tmp_file, '/etc/systemd/system/simplyprint-connector.service'])

    # Reload the systemd daemon
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'])

    # Enable the service
    subprocess.run(['sudo', 'systemctl', 'enable', 'simplyprint-connector'])

    # Start the service
    subprocess.run(['sudo', 'systemctl', 'start', 'simplyprint-connector'])

    print('The Simplyprint Connector has been installed as a systemd service.')
