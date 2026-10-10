"""Providers cannot pretend that an unsupported view is an empty wallet."""


class CapabilityUnavailable(RuntimeError):
    pass
