"""有界波形历史：环形存储，随机访问与可视区切片不遍历全部历史。"""

from itertools import chain, islice
import operator


class PointHistory:
    """与 deque 相同的尾部保留语义，并提供 O(1) 索引用于二分查找。"""

    def __init__(self, points=(), maxlen=200000):
        self.maxlen = max(1, int(maxlen))
        self._items = [None] * self.maxlen
        self._head = 0
        self._count = 0
        self.extend(points)

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._count)
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            size = max(0, stop - start)
            offset = (self._head - self._count + start) % self.maxlen
            first = min(size, self.maxlen - offset)
            return self._items[offset:offset + first] + self._items[:size - first]
        index = operator.index(index)
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        return self._items[(self._head - self._count + index) % self.maxlen]

    def __iter__(self):
        start = (self._head - self._count) % self.maxlen
        first = min(self._count, self.maxlen - start)
        return chain(islice(self._items, start, start + first),
                     islice(self._items, 0, self._count - first))

    def extend(self, points):
        for point in points:
            self._items[self._head] = point
            self._head = (self._head + 1) % self.maxlen
            self._count = min(self._count + 1, self.maxlen)

    def clear(self):
        self._items = [None] * self.maxlen
        self._head = self._count = 0
