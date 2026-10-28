"""Project paths shared by the parallel-network runtime modules."""

from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
CONFIG_DIR = str(Path(PROJECT_ROOT) / 'configs')
BUILD_DIR = str(Path(PROJECT_ROOT) / 'build')
DEFAULT_LOG_DIR = str(Path(BUILD_DIR) / 'logs')
