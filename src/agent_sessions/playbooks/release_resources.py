"""Exact release resources; only these bytes may be read from uv cache hard links.

Update the hashes when changing a shipped example. Installed-wheel tests pin the inventory.
"""

FILES = {
    "forgejo-workflow": {
        "README.md": "c41dc68b0e9e748355528cee57e0f02d0f19760313fc3fe3389eaf6bb0a828ee",
        "flows/issue-to-live.toml": (
            "957a6b20f82fc20b6e3020934b05e61aa4fa39309828794a1799b6289b1dab86"
        ),
        "playbook.toml": "8e3949d7a29fd6a54cb7a30146f5662b1e0e4e19f061d5a05f057cd33761f96a",
    },
    "research-brief": {
        "README.md": "07a24b29dadcbee236288446dc756953061e61a20ef5662a738300474fd88f1f",
        "flows/research.toml": "6ef373c23dd977f8684563168c133e44148e6c5e4efd2c466d1a23392e2147d1",
        "playbook.toml": "9e0fb8cd47037713d5581a109153cb8dbbe770a885f67e3be76aeaa6ab19b88f",
    },
}
