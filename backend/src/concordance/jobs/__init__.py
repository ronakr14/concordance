"""The job queue, the worker loop, and what a job can be.

`registry` holds the kind-to-handler map, `worker` the loop that claims and
runs them, `scheduler` the in-house interval scheduler, `handlers` the five
kinds themselves. `reconcile`, `replay` and `diff` are the operations those
handlers and the CLI share - see docs/orchestration.md.
"""
