# become-py
Python library for lazy running &amp; caching callables onto a ctx dict

## Installation
pip install become

## Usage
```python
```

### Example usage

```python
from become import be, be_singleton

hello = be(lambda ctx: "Hello")
world = be(lambda ctx: "World")
greeting = be(lambda ctx: f"{hello(ctx)} {world(ctx)}!")

ctx = {}
print(greeting(ctx))  # "Hello World!"
```
