"""Built-in commands. Each module here registers its commands when imported;
every module is imported."""

from .._autoload import import_all

import_all(__name__, __path__)
