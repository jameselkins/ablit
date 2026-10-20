"""Engine template rendering.

The templates themselves live in engines/<name>/ at the top of the repository
and stay there as data this package reads. Each has bootstrap.sh, health.sh and
lock.yaml.

TODO(step 3): render the variable files, the artifact list and the engine
config from a catalog entry, and copy the scripts to the rented machine.
"""
