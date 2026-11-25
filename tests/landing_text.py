"""Visible legal text of a landing page, for the "the wording does not change" tests (#1597).

The policy text is what `[data-legal-text]` (or, on the pre-NIGHTJAR pages, `section.panel`)
contains, minus scripts, styles and the analytics control (`#consent-settings`, which consent.js
owns and renders). Case and whitespace are normalised: letter case is a CSS concern on these pages
(`text-transform`), so `BATTLELAB // LEGAL` and `BattleLab // legal` are the same words.
"""

from html.parser import HTMLParser

_VOID = {
    "br", "img", "meta", "link", "input", "hr", "source",
    "wbr", "area", "base", "col", "embed", "track",
}  # fmt: skip


class _Extract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0  # >0 while inside the legal container
        self.skip = 0  # >0 while inside script/style/the consent control
        self.out: list[str] = []

    def _is_root(self, tag: str, attrs: dict) -> bool:
        if "data-legal-text" in attrs:
            return True
        return tag == "section" and "panel" in (attrs.get("class") or "").split()

    def handle_starttag(self, tag, attrs_list):
        attrs = dict(attrs_list)
        if tag in _VOID:
            if self.depth and not self.skip and tag == "br":
                self.out.append(" ")
            return
        if self.depth:
            self.depth += 1
            if self.skip or tag in ("script", "style") or attrs.get("id") == "consent-settings":
                self.skip += 1
        elif self._is_root(tag, attrs):
            self.depth = 1

    def handle_endtag(self, tag):
        if tag in _VOID or not self.depth:
            return
        if self.skip:
            self.skip -= 1
        self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.out.append(data)


def legal_text(html: str) -> str:
    p = _Extract()
    p.feed(html)
    return " ".join("".join(p.out).split()).casefold()
