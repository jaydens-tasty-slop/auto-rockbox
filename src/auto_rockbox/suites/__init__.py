"""Built-in test suites and their scenarios (auto_rockbox.engine); every
module here is imported, and registers its scenarios and commands."""

from .._autoload import import_all

import_all(__name__, __path__)
