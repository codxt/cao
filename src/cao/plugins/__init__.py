"""Runtime plugin implementations for CAO.

Each subpackage under this package is one runtime integration. The core never
imports these modules; a plugin is selected explicitly by id through
:class:`cao.runtime.PluginRegistry`.
"""
