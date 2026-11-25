"""The gateway log names the functions that asked for a request, innermost first, so that a model
load can be traced to its cause (the summary check, the second look, a chat)."""

from topcoat_hub.gateway import caller_chain


def _outer() -> str:
    return _inner()


def _inner() -> str:
    return caller_chain()


def test_caller_chain_names_the_asking_functions_innermost_first() -> None:
    chain = _outer()
    assert chain.startswith("_inner<-_outer<-test_caller_chain_names_the_asking_functions")


def test_caller_chain_is_short() -> None:
    assert len(_outer().split("<-")) <= 6
