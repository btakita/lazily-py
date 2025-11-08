from become import be, be_singleton


def be_foo(ctx: dict) -> int:
    return 1

be_foo = be(be_foo)

class be_bar(be_singleton[int]):
    def callable(self, ctx: dict) -> int:
        return be_foo(ctx) + 100

if __name__ == "__main__":
    ctx = dict()
    assert be_foo(ctx) == 1
    assert be_bar(ctx) == 101, (be_bar(ctx), 101)
    print("Success!")
