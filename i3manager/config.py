import tomllib

from xdg import BaseDirectory as basedir


class Configuration:
    def __init__(self):
        self.read()

    def read(self):
        filename = basedir.load_first_config('i3/manager.cfg')
        if not filename:
            return
        with open(filename, 'rb') as fp:
            config = tomllib.load(fp)

        self.outputs = config['outputs']


config = Configuration()
