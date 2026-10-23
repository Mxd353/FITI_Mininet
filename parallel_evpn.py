#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI entrypoint for the parallel-network EVPN route model."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parallel.evpn import main


if __name__ == '__main__':
    sys.exit(main())
