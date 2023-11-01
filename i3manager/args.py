import argparse
import pathlib

from xdg import BaseDirectory as basedir


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', help='Enable debug loggin', action='store_true')
    parser.add_argument('--socket', help='Command socket (%(default)s)',
            default=str(pathlib.Path(basedir.get_runtime_dir()) / 'i3-manager.socket'))
    return parser.parse_args()


args = parse_args()
