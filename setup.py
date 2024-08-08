# -*- coding: utf-8 -*-

import os
import versioneer
from setuptools import setup, find_packages

PACKAGE_NAME = "meltingplot.duet_simplyprint_connector"

REQUIREMENTS = []
with open(os.path.join(os.path.dirname(__file__), 'requirements.txt')) as f:
    for line in f:
        REQUIREMENTS.append(line.strip())

REQUIREMENTS_TEST = []
with open(
        os.path.join(os.path.dirname(__file__), 'requirements_test.txt')) as f:
    for line in f:
        REQUIREMENTS_TEST.append(line.strip())


with open('README.rst') as f:
    README = f.read()

with open('LICENSE') as f:
    LICENSE = f.read()

setup(
    name=PACKAGE_NAME,
    description='Simplyprint.io connector for 3D printers running Duet firmware.',
    long_description=README,
    long_description_content_type='text/x-rst',
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
    author='Tim Schneider',
    author_email='tim@meltingplot.net',
    url='',
    license=LICENSE,
    packages=find_packages(exclude=('tests', 'docs', 'venv')),
    include_package_data=True,
    zip_safe=False,
    install_requires=REQUIREMENTS,
    extras_require={
        'test': REQUIREMENTS_TEST,
    },
    entry_points={
        'console_scripts': [
            "simplyprint = meltingplot.duet_simplyprint_connector.__main__:main",
        ]
    },
    data_files=[
        ('', [
            'simplyprint-connector.service',
            ]),
    ]
)
