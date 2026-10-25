"""进程内单例装配的回归测试（嵌套初始化不能死锁）。"""

from __future__ import annotations

import threading

from data_interface.api import runtime


def test_nested_singleton_factory_does_not_deadlock():
    old_state = dict(runtime._state)
    old_locks = dict(runtime._creation_locks)
    runtime._state.clear()
    runtime._creation_locks.clear()
    result: list[object] = []

    def worker():
        result.append(runtime._get("outer", lambda: runtime._get("inner", lambda: 42)))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(2.0)

    runtime._state.clear()
    runtime._state.update(old_state)
    runtime._creation_locks.clear()
    runtime._creation_locks.update(old_locks)

    assert not thread.is_alive(), "嵌套初始化发生死锁"
    assert result == [42]
