"""Filter engine — declarative specs routed over the survivor pool (issue #146).

`ENGINE_VERSION` is bumped whenever routing behavior changes *without* a spec
change; it is one of the inputs to the routing release id, so a behavior change
that left every spec file byte-identical still produces a new release.
"""

ENGINE_VERSION = "1"
