#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI compatibility entrypoint for :mod:`fiti.slice`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fiti.slice import *  # noqa: F401,F403
from fiti.slice import main


if __name__ == '__main__':
    sys.exit(main())
