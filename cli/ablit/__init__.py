"""ablit finds the cheapest rented GPU machine that can run a model.

It compares marketplace offers, starts a pinned model through an SSH tunnel,
and confirms that billing has stopped when the run is destroyed.
"""

__version__ = "0.1.3"
