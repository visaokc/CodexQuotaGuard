"""The production client uses only the embedded Tailscale transport."""
from .tsnet_mesh import TailscaleMesh


def make_mesh(config, account, on_message):
    return TailscaleMesh(config, account, on_message)
