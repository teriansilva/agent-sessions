"""Exact release resources; only these bytes may be read from uv cache hard links.

Update the hashes when changing a shipped example. Installed-wheel tests pin the inventory.
"""

FILES = {
    "forgejo-workflow": {
        "README.md": "2a24a654a4a78c961252781384a2d909ac5366238e7ffba9f60cd7a75e0f4c35",
        "flows/issue-to-live.toml": (
            "4f38a33a97506642ad10ae6395b853ca14cbc0eb1f29f0afcbd69f6eef5bb090"
        ),
        "playbook.toml": "23869c4cf8f9e94856400b228ddf738e1518eae8f60d83e7c14aba670a397f8e",
    },
    "research-brief": {
        "README.md": "71c484cd4fabf299ac87dc185ba0de9a0cf378f5bf0fc6c84dbd3accafcd4ead",
        "flows/research.toml": "f6a09424d2166fc4ab6b13e913f0745751bcf5bbe5ce2de4d15800eaa0ba1e3f",
        "playbook.toml": "de1c7eb131697eaa389d1c4cf6bfc3056b2a0f86d26fa53f7d691ec614a73daa",
    },
}
