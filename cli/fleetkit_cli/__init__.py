"""fleetkit: one deploy path for an estate repo, as a command line and an HTTP API.

An estate is provisioned by Pulumi (the programs the estate repo renders from
the model as `pulumi.<estate>.<stack>`) and configured by Colmena (the hive it
renders as `hives.<hive>`). Both are only run here; what they declare is
evaluated by Nix, never written in Python.
"""
