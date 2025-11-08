from typing import Any, Callable, Dict, Generic, Optional, Type, TypeVar


__all__ = ["Be", "be", "be_class"]

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
    hello = be(lambda ctx: "Hello")
    world = be(lambda ctx: "World")

    greeting = be(lambda ctx: f"{hello(ctx)} {world(ctx)}!")

    ctx = {}
    greeting(ctx)  # Hello World!
    ```
    """

    def __init__(self, callable: Callable[[dict], T]) -> None:
        self.callable = callable


class Meta_be_class(type):
    """Metaclass that enables singleton behavior and direct calling with ctx."""

    _instances: Optional[Dict[Type, Any]] = None

    def __call__(cls, ctx: dict) -> Any:
        """Allow calling the class directly with ctx: be_foo(ctx)"""
        # Call the singleton instance with the context
        return cls.instance(ctx)

    @property
    def instance(cls) -> Any:
        if cls._instances is None:
            cls._instances = {}

        if cls not in cls._instances:
            # Create the singleton instance
            instance = type.__call__(cls)
            cls._instances[cls] = instance

        return cls._instances[cls]


class be_class(Be[T], metaclass=Meta_be_class):
    """
    A Be that is a singleton. callable is defined as a method.
    Uses a metaclass to enable direct calling with context.

    Usage:
    ```
    class hello(be_class[str]):
        def callable(self, ctx: dict) -> str:
            return "Hello"


    class world(be_class[str]):
        def callable(self, ctx: dict) -> str:
            return "World!"


    class greeting(be_class[str]):
        def callable(self, ctx: dict) -> str:
            return f"{hello(ctx)} {world(ctx)}!"


    ctx = {}
    result = greeting(ctx)  # Hello World!
    ```
    """

    def __init__(self) -> None:
        # Initialize without arguments since the metaclass handles singleton creation
        pass

    def callable(self, ctx: dict) -> T:
        raise NotImplementedError

    @classmethod
    def get(cls, ctx: dict) -> Optional[T]:
        """Allow calling get directly on the class"""
        return Be.get(cls.instance, ctx)

    @classmethod
    def is_in(cls, ctx: dict) -> bool:
        """Allow calling is_in directly on the class"""
        return Be.is_in(cls.instance, ctx)
