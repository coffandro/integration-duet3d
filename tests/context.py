# -*- coding: utf-8 -*-

import sys
import os
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from meltingplot.duet_simplyprint_connector.duet.api import RepRapFirmware  # noqa
