"""The chat engine, moved out of :mod:`topcoat_hub.api.chat` (issue #205).

- :mod:`.turn`: what one answer needs (``Turn``) and how it is read (``prepare``)
- :mod:`.pipeline`: what a message turns into, from the node's reservation to the stream
- :mod:`.answer`: the steps of an answer (the model's answer, the checks, the hand-overs)
- :mod:`.common`: helpers the routes and the engine share

The routes stay in :mod:`topcoat_hub.api.chat`; the engine never imports them."""
