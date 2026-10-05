"""Router drivers. Add a new router family by writing a Driver subclass and listing it here."""

from .base import Driver, NotLoggedIn, RouterError
from .realtek_boa import RealtekBoa

DRIVERS = {
    'realtek-boa': RealtekBoa,
}


def make_driver(name, host):
    try:
        return DRIVERS[name](host)
    except KeyError:
        raise SystemExit('Unknown router driver %r. Known: %s' % (name, ', '.join(sorted(DRIVERS))))


__all__ = ['DRIVERS', 'Driver', 'NotLoggedIn', 'RouterError', 'make_driver']
