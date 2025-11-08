import pytest

from lazily import Be, be, be_class


class TestBe:
    """Test the base Be class functionality."""

    def test_be_abstract_nature(self):
        """Test that Be cannot be instantiated directly without callable."""
        with pytest.raises(AttributeError):
            be_instance = Be()
            be_instance({})

    def test_be_with_manual_callable(self):
        """Test Be with manually assigned callable."""
        be_instance = Be()
        be_instance.callable = lambda ctx: "test value"

        ctx = {}
        result = be_instance(ctx)

        assert result == "test value"
        assert be_instance in ctx
        assert ctx[be_instance] == "test value"

    def test_be_get_method(self):
        """Test the get method."""
        be_instance = Be()
        be_instance.callable = lambda ctx: "test value"

        ctx = {}

        # Should return None when not in context
        assert be_instance.get(ctx) is None

        # Should return value after calling
        be_instance(ctx)
        assert be_instance.get(ctx) == "test value"

    def test_be_is_in_method(self):
        """Test the is_in method."""
        be_instance = Be()
        be_instance.callable = lambda ctx: "test value"

        ctx = {}

        # Should not be in context initially
        assert not be_instance.is_in(ctx)

        # Should be in context after calling
        be_instance(ctx)
        assert be_instance.is_in(ctx)


class TestBeClass:
    """Test the be class functionality."""

    def test_simple_be(self):
        """Test basic be functionality."""
        be_hello = be(lambda ctx: "Hello")

        ctx = {}
        result = be_hello(ctx)

        assert result == "Hello"
        assert be_hello in ctx
        assert ctx[be_hello] == "Hello"

    def test_be_caching(self):
        """Test that be caches results."""
        call_count = 0

        def increment_and_return():
            nonlocal call_count
            call_count += 1
            return f"called {call_count} times"

        be_counter = be(lambda ctx: increment_and_return())

        ctx = {}

        # First call
        result1 = be_counter(ctx)
        assert result1 == "called 1 times"
        assert call_count == 1

        # Second call should return cached value
        result2 = be_counter(ctx)
        assert result2 == "called 1 times"
        assert call_count == 1  # Should not increment

    def test_be_dependency_chain(self):
        """Test be objects depending on other be objects."""
        be_first = be(lambda ctx: "Hello")
        be_second = be(lambda ctx: "World")
        be_combined = be(lambda ctx: f"{be_first(ctx)} {be_second(ctx)}!")

        ctx = {}
        result = be_combined(ctx)

        assert result == "Hello World!"
        assert be_first in ctx
        assert be_second in ctx
        assert be_combined in ctx

    def test_multiple_contexts(self):
        """Test that different contexts are independent."""
        be_value = be(lambda ctx: len(ctx))

        ctx1 = {}
        ctx2 = {"existing": "value"}

        result1 = be_value(ctx1)
        result2 = be_value(ctx2)

        assert result1 == 0
        assert result2 == 1

    def test_be_with_complex_types(self):
        """Test be with complex return types."""
        be_dict = be(lambda ctx: {"key": "value", "number": 42})
        be_list = be(lambda ctx: [1, 2, 3])

        ctx = {}

        dict_result = be_dict(ctx)
        list_result = be_list(ctx)

        assert dict_result == {"key": "value", "number": 42}
        assert list_result == [1, 2, 3]


class TestBeSingleton:
    """Test the be_class functionality."""

    def test_simple_singleton(self):
        """Test basic singleton functionality."""

        class be_hello(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return "Hello from singleton"

        ctx = {}
        result = be_hello(ctx)

        assert result == "Hello from singleton"

        # Check that it's cached in context
        assert len([k for k in ctx if hasattr(k, "callable")]) == 1

    def test_singleton_behavior(self):
        """Test that multiple calls create the same singleton instance."""

        class be_counter(be_class[int]):
            def __init__(self):
                super().__init__()
                self.count = 0

            def callable(self, ctx: dict) -> int:
                self.count += 1
                return self.count

        ctx1 = {}
        ctx2 = {}

        # First call should create instance and return 1
        result1 = be_counter(ctx1)
        assert result1 == 1

        # Second call with different context should use same instance
        # but since it's cached in ctx1, it should return cached value in new context
        result2 = be_counter(ctx2)
        assert result2 == 2  # Instance count incremented

        # Calling again with ctx1 should return cached value
        result3 = be_counter(ctx1)
        assert result3 == 1  # Cached value from ctx1

    def test_singleton_dependency_chain(self):
        """Test singleton objects depending on other singleton objects."""

        class be_base(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return "base"

        class be_derived(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return f"{be_base(ctx)}_derived"

        ctx = {}
        result = be_derived(ctx)

        assert result == "base_derived"

    def test_multiple_singleton_classes(self):
        """Test that different singleton classes are independent."""

        class be_foo(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return "foo"

        class be_bar(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return "bar"

        ctx = {}

        foo_result = be_foo(ctx)
        bar_result = be_bar(ctx)

        assert foo_result == "foo"
        assert bar_result == "bar"

        # Both should be cached in context
        assert len([k for k in ctx if hasattr(k, "callable")]) == 2

    def test_singleton_with_complex_logic(self):
        """Test singleton with more complex callable logic."""

        class be_fibonacci(be_class[int]):
            def callable(self, ctx: dict) -> int:
                # Simple fibonacci calculation
                n = ctx.get("fib_n", 10)
                if n <= 1:
                    return n

                a, b = 0, 1
                for _ in range(2, n + 1):
                    a, b = b, a + b
                return b

        ctx = {"fib_n": 7}
        result = be_fibonacci(ctx)

        assert result == 13  # 7th fibonacci number

    def test_singleton_inheritance_error(self):
        """Test that singleton raises NotImplementedError if callable not implemented."""

        class be_abstract(be_class[str]):
            pass  # No callable method implemented

        ctx = {}

        with pytest.raises(NotImplementedError):
            be_abstract(ctx)


class TestIntegration:
    """Integration tests combining different be types."""

    def test_mixed_be_types(self):
        """Test mixing regular be and be_class."""
        # Regular be
        be_regular = be(lambda ctx: "regular")

        # Singleton be
        class be_single(be_class[str]):
            def callable(self, ctx: dict) -> str:
                return "singleton"

        # Combined
        be_combined = be(lambda ctx: f"{be_regular(ctx)} + {be_single(ctx)}")

        ctx = {}
        result = be_combined(ctx)

        assert result == "regular + singleton"

    def test_complex_dependency_graph(self):
        """Test a complex dependency graph."""
        be_config = be(
            lambda ctx: {"api_url": "https://api.example.com", "timeout": 30}
        )

        class be_http_client(be_class[str]):
            def callable(self, ctx: dict) -> str:
                config = be_config(ctx)
                return f"HttpClient({config['api_url']}, timeout={config['timeout']})"

        be_user_service = be(lambda ctx: f"UserService({be_http_client(ctx)})")
        be_auth_service = be(lambda ctx: f"AuthService({be_http_client(ctx)})")

        be_app = be(
            lambda ctx: f"App(user={be_user_service(ctx)}, auth={be_auth_service(ctx)})"
        )

        ctx = {}
        result = be_app(ctx)

        expected = "App(user=UserService(HttpClient(https://api.example.com, timeout=30)), auth=AuthService(HttpClient(https://api.example.com, timeout=30)))"
        assert result == expected

    def test_context_isolation(self):
        """Test that different contexts don't interfere with each other."""
        be_value = be(lambda ctx: ctx.get("input", "default"))

        class be_multiplier(be_class[int]):
            def callable(self, ctx: dict) -> int:
                base = be_value(ctx)
                return len(base) * 2

        ctx1 = {"input": "hello"}
        ctx2 = {"input": "hi"}
        ctx3 = {}

        result1 = be_multiplier(ctx1)  # len('hello') * 2 = 10
        result2 = be_multiplier(ctx2)  # len('hi') * 2 = 4
        result3 = be_multiplier(ctx3)  # len('default') * 2 = 14

        assert result1 == 10
        assert result2 == 4
        assert result3 == 14


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_context(self):
        """Test behavior with empty context."""
        be_simple = be(lambda ctx: "value")

        ctx = {}
        result = be_simple(ctx)

        assert result == "value"
        assert len(ctx) == 1

    def test_context_mutation(self):
        """Test that be objects can read from context mutations."""
        be_reader = be(lambda ctx: ctx.get("dynamic_value", "not_found"))

        ctx = {}

        # First call - value not in context
        result1 = be_reader(ctx)
        assert result1 == "not_found"

        # Add value to context
        ctx["dynamic_value"] = "found"

        # Create new be that reads the same key
        be_reader2 = be(lambda ctx: ctx.get("dynamic_value", "not_found"))
        result2 = be_reader2(ctx)
        assert result2 == "found"

        # Original be_reader should still return cached value
        result3 = be_reader(ctx)
        assert result3 == "not_found"  # Cached result

    def test_none_values(self):
        """Test handling of None values."""
        be_none = be(lambda ctx: None)

        ctx = {}
        result = be_none(ctx)

        assert result is None
        assert be_none.is_in(ctx)
        assert be_none.get(ctx) is None

    def test_exception_in_callable(self):
        """Test behavior when callable raises an exception."""
        be_error = be(lambda ctx: 1 / 0)  # Division by zero

        ctx = {}

        with pytest.raises(ZeroDivisionError):
            be_error(ctx)

        # Should not be cached after exception
        assert not be_error.is_in(ctx)
