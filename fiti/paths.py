"""Project paths shared by the FITI modules.

The repository is intentionally runnable without installing the package, so
all default paths are derived from this file instead of the current working
directory.
"""

from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
CONFIG_DIR = str(Path(PROJECT_ROOT) / 'configs')
BUILD_DIR = str(Path(PROJECT_ROOT) / 'build')

DEFAULT_TOPO_FILE = str(Path(CONFIG_DIR) / 'fiti.json')
DEFAULT_SLICES_FILE = str(Path(CONFIG_DIR) / 'slices.json')
DEFAULT_P4_SRC = str(Path(PROJECT_ROOT) / 'p4src' / 'fiti_slice.p4')
DEFAULT_P4_JSON = str(Path(BUILD_DIR) / 'fiti_slice.json')
DEFAULT_P4INFO = str(Path(BUILD_DIR) / 'fiti_slice.p4info.txt')
DEFAULT_LOG_DIR = str(Path(BUILD_DIR) / 'logs')
DEFAULT_PCAP_DIR = str(Path(PROJECT_ROOT) / 'pcap')
