from be import Be


def be_foo(ctx: dict) -> int:
    return 1

be_foo = Be.new(be_foo)

class be_bar(Be[int]):
    def callable(self, ctx: dict) -> int:
        return be_foo(ctx) + 100

if __name__ == "__main__":
    ctx = dict()
    assert be_foo(ctx) == 1
    assert be_bar(ctx) == 101
    print("Success!")
