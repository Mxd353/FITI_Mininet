#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI entrypoint for :mod:`fiti.ctl`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fiti.ctl import main


if __name__ == '__main__':
    sys.exit(main())
