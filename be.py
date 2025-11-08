from typing import Callable, Generic, Optional, TypeVar

__all__ = ["Be", "be", "be_singleton"]

T = TypeVar("T")

class Be(Generic[T]):
    """
    Base class for a lazy be Callable. Wraps a callable implementation field.

    If the be is not in the ctx argument, it will be evaluated and stored in the ctx.
    """
    callable: Callable[[dict], T]

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

class be(Be[T]):
    """
    A Be that can be initialized with the callable as an argument.

    Usage:
    ```
    be_hello = be(lambda ctx: "Hello")

    be_hello_world = be(lambda ctx: be_hello(ctx) + " World!)
    ```
    """
    def __init__(self, callable: Callable[[dict], T]) -> None:
        self.callable = callable

class be_singleton(Be[T]):
    """
    A Be that is a singleton. callable is defined as a method.

    Usage:
    ```
    class be_hello(be_singleton[int]):
        def callable(self, ctx: dict) -> int:
            return "Hello"

    class be_hello_world(be_singleton[int]):
        def callable(self, ctx: dict) -> int:
            return be_hello(ctx) + " World!
    ```
    """
    instance: Optional[Be[T]] = None

    def __new__(cls, ctx: dict) -> T:
        if cls.instance is None:
            cls.instance = super().__new__(cls)

        return cls.instance(ctx)

    def callable(self, ctx: dict) -> T:
        raise NotImplementedError
