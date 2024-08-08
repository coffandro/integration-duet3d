#!/bin/bash

set -e

source yapf-module.sh
source flake8-module.sh

python3 setup.py bdist_wheel