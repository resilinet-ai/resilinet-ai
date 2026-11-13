"""RAI chaos agent (M07).

The only component permitted to inject faults. It re-runs every preflight check itself,
denies by default, enforces a maximum fault duration and owns the kill-switch
(Architecture section 3). Implementation arrives with backlog items B-23 to B-25.
"""

__version__ = "0.0.1"
