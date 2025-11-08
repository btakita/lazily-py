
from typing import Callable, Generic, Optional, TypeVar

__all__ = ["Be"]

T = TypeVar("T")

class BeMeta(type):
    def __call__(cls, *args, **kwargs):
        # If called with a dict argument, treat as class method call
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            # Create a temporary instance and call it
            instance = super().__call__()
            if hasattr(instance, 'callable'):
                return instance(args[0])
            else:
                raise NotImplementedError("Class must implement callable method")
        # Otherwise, normal constructor call
        return super().__call__(*args, **kwargs)

class Be(Generic[T], metaclass=BeMeta):
    callable: Callable[[dict], T]

    @classmethod
    def new(cls, callable: Callable[[dict], T]) -> "Be":
        be = Be[T]()
        be.callable = callable
        return be

    def __call__(self, ctx: dict) -> T:
        if self in ctx:
            return ctx[self]
        else:
            ctx[self] = self.callable(ctx)
            return ctx[self]

    def get(self, ctx: dict) -> Optional[T]:
        return ctx.get(self)

    def is_in(self, ctx: dict) -> bool:
        return self in ctx