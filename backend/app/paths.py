"""Shared Community data path, safe to import without opening a Store."""
import os
import sys
from pathlib import Path


def default_data_directory():
    """Use a separate Community namespace; never attach to the original demo."""
    if os.environ.get('TUXU_DATA_DIR'):
        return Path(os.environ['TUXU_DATA_DIR']).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'TuxuCommunity' / 'data'
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'TuxuCommunity' / 'data'

