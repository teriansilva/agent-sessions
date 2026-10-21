"""The one error a playbook bundle raises."""

from __future__ import annotations

from ..plugins.manifest import ManifestError


class PlaybookFormatError(ManifestError):
    """A bundle that does not validate. `field` names where (a file, then a dotted path into it).

    A `ManifestError` subclass because the strict table reader is the plugin manifest's
    (`manifest._Reader`); its errors are re-raised as this type with the file prefixed, so a
    caller only ever catches one class.
    """
