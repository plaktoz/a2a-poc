"""A plugin that does not satisfy the interface: its run() takes no prompt. For test_workers.py."""


class Plugin:
    name = "bad"

    def __init__(self, cfg, worker):
        pass

    @classmethod
    def validate(cls, cfg, worker):
        pass

    def run(self):
        return "x", {}
