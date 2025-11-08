# lazily
A Python library for lazy evaluation with context caching.

## Installation
pip install lazily

## Usage
```python
```

### Example usage

```python
from lazily import be, be_class

hello = be(lambda ctx: "Hello")
world = be(lambda ctx: "World")
greeting = be(lambda ctx: f"{hello(ctx)} {world(ctx)}!")

ctx = {}
print(greeting(ctx))  # "Hello World!"
```
