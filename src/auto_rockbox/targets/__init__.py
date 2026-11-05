"""Built-in players: one subpackage per target, each registering its
`Target` subclass and its own commands when imported; every one is imported."""

from .._autoload import import_all

import_all(__name__, __path__)
