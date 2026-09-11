try:
    from .cli import cli  # noqa
except ImportError:
    pass

try:
    from .spider import CrauSpider  # noqa
except ImportError:
    pass

from .version import __version__  # noqa

