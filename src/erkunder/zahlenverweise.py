"""Collect explicit Markdown links before interpreting any displayed number."""
from collections.abc import Iterator
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode


def zahlenverweise(text: str) -> list[tuple[str, str]]:
    tree = SyntaxTreeNode(MarkdownIt("commonmark").parse(text))
    links = []
    for node in _aktive_knoten(tree):
        if node.type != "link":
            continue
        target = str(node.attrs["href"])
        scheme, _, ident = target.partition(":")
        if scheme.casefold() == "zahl":
            links.append((_linktext(node), unquote(ident)))
    return links


def _aktive_knoten(node: SyntaxTreeNode) -> Iterator[SyntaxTreeNode]:
    # Image descendants describe alt text; their link tokens are not rendered as anchors.
    if node.type == "image":
        return
    yield node
    for child in node.children:
        yield from _aktive_knoten(child)


def zahl_im_zitat(shown: str, quote: str) -> bool:
    """Validate a reviewer-supplied quote using the same visible text as the links."""
    tree = SyntaxTreeNode(MarkdownIt("commonmark").parseInline(quote))
    return shown in _linktext(tree)


def _linktext(node: SyntaxTreeNode) -> str:
    if node.type in ("text", "code_inline"):
        return node.content
    if node.type in ("softbreak", "hardbreak"):
        return "\n"
    if node.type in ("image", "html_inline"):
        # Preserve non-text labels as visibly unreadable, never as a valid number.
        return f"<{node.type}:{node.content}>"
    return "".join(_linktext(child) for child in node.children)
