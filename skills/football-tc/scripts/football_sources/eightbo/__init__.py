"""8BO browser-first acquisition adapter."""

__all__ = ["EightBOCollector", "collect_8bo"]


def __getattr__(name: str):
    if name in __all__:
        from .runner import EightBOCollector, collect_8bo

        return {"EightBOCollector": EightBOCollector, "collect_8bo": collect_8bo}[name]
    raise AttributeError(name)
