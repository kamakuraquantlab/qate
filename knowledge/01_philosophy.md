## 1 Don't check NONE

This codebase follows a **fail-fast philosophy**. Two types of errors:

### 1.1 Static Errors (Logic Bugs)

**Don't check at runtime** — catch in tests/backtest.

```python
chart.get_values(200)  # invalid index (max_len=120)
order_list[5]          # non-existent order
strategy.gateway       # gateway not yet set
```

Don't add checks out of worry about crashes. Either trace the code to confirm it's safe, or let it crash with a clear stack trace. That is better than silently allowing a critical component to be in an invalid state.

### 1.2 Dynamic Errors (Runtime Conditions)

**Do check at runtime** — happen due to external factors regardless of code correctness.

```python
bar.volume / bar.tick_count  # tick_count could be 0
order_book.best_bid          # could be None
api_response.json()          # could fail (network/exchange)
```

```python
"avg_size": self.volume / self.tick_count if self.tick_count > 0 else 0.0
```

## 2 Don't write tests

Test coverage convinces people who don't understand the code. Most test cases are a waste — to write and later to maintain.

We test edge cases only. An edge case is **not** abnormal, null, or wrong input — it's something that must happen in production but doesn't happen every time. Everything else runs in production every second -- is always tested by the prodcution.

For example, we have tests for websocket auto-reconnect and trade reconciliation, but not for the event loop.

For tests, I will start with a simple version describing what to test, then let you complete it.

## 3 Never use `time.time()` in strategies

Strategies must not call `time.time()`. Use `self.now_ts` instead, which is set from incoming event timestamps (e.g. `order_book.get_ts()`).

```python
# Wrong — breaks backtesting
now = time.time()

# Right — reproducible across live and backtest
self.now_ts = order_book.get_ts()
```

In production, `order_book.get_ts()` is effectively real-time (books arrive continuously). In backtesting, it returns the recorded event timestamp, making replay deterministic. Using `time.time()` in a strategy breaks that guarantee.

Pattern: set `self.now_ts` at the top of `handle_order_book` (and `warmup_order_book`). Use it everywhere in the strategy that needs a current timestamp.

## 4 Don't write comments to explain code

Don't comment on what can be inferred from the code. If the code isn't self-evident, improve the code or the design until it is.

Write comments only for the *idea behind* the code — never for the code itself.

For comments, I will start with a rough version, then let you improve the narrative.
