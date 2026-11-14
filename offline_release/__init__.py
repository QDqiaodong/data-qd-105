"""Offline release CLI for air-gapped survey workstations.

Pure standard-library package. Three subcommands:

* ``pack``    collect project wheel + locked runtime wheels + pip + synthetic
              inputs from a prepared wheel staging directory into a self
              describing release bundle,
* ``verify``  re-check a bundle: manifest hashes, wheel metadata, dependency
              closure and target (CPython minor / OS / architecture) tags,
* ``install`` create an isolated venv with a given interpreter (no pip needed
              on that interpreter) and install the bundle fully offline.

Nothing in this package imports anything beyond the standard library so the
same code runs on a stripped target interpreter that ships without pip.
"""

__version__ = "1.0.0"
MANIFEST_NAME = "MANIFEST.json"
WHEELS_DIR = "wheels"
INPUTS_DIR = "inputs"
META_DIR = "meta"
TOOL_DIR = "offline_release"
