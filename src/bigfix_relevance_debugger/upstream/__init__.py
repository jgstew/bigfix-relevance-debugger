"""Code written to be donated to the libraries this app builds on.

One module per library: ``remote_client_relevance`` for
bigfix-remote-client-relevance, and ``relevance_analyzer`` for
bigfix-relevance-analyzer when there is something to donate to it. Rules that keep a move a copy-paste:

* Import only the stdlib and *that* library's public API (its package
  ``__init__`` or a submodule's ``__all__``) -- never this app's modules,
  never NiceGUI, never the other library.
* Use the library's own vocabulary (its platform keys, its outcome names).
  Translating between the two libraries is app glue and stays outside here.
* Tests live in ``tests/unit/upstream/`` under the same rules, so they move
  with the module.

Once a library ships an equivalent, delete the function here and call the
library's.
"""
