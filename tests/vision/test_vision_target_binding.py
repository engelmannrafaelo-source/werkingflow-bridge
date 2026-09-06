"""Der 400-Waechter fuer ``gemini_thinking_budget`` darf ``_vision_target`` nicht
lesen, bevor ``chat_completions`` den Namen gebunden hat.

Warum als AST-Test und nicht als Aufruf gegen den Endpunkt: der Fehler ist eine
Scoping-Falle, keine Logikfrage. ``_vision_target`` wird weiter unten in
derselben Funktion zugewiesen — damit ist der Name in der GANZEN Funktion lokal,
und der frueher stehende Waechter lief in einen ``UnboundLocalError``. Der
Aufrufer haette also fuer jeden Aufruf mit ``gemini_thinking_budget`` einen 500
bekommen ("die Bridge ist kaputt") statt entweder des vorgesehenen 400 oder,
auf dem Gemini-Weg, seiner Messung. Genau die Aufrufe, fuer die das Feld gebaut
wurde (Denken an/aus im selben Fenster), waren die einzigen betroffenen.

Der Test prueft die Bedingung, die Python selbst prueft: erste Bindung vor
erstem Lesen. Ein Endpunkt-Test wuerde denselben Fehler finden, aber die halbe
Auth- und Middleware-Kette mitschleppen und beim naechsten Umbau aus einem
anderen Grund rot werden.
"""
from __future__ import annotations

import ast
import pathlib


def _chat_completions_node() -> ast.AsyncFunctionDef:
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    tree = ast.parse(src.read_text())
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_completions":
            return node
    raise AssertionError("chat_completions nicht in src/main.py gefunden")


def test_vision_target_is_bound_before_it_is_read():
    fn = _chat_completions_node()
    stores, loads = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "_vision_target":
            (stores if isinstance(node.ctx, ast.Store) else loads).append(node.lineno)

    assert stores, "_vision_target wird in chat_completions nie zugewiesen"
    assert loads, "_vision_target wird in chat_completions nie gelesen"
    assert min(stores) < min(loads), (
        f"_vision_target wird in Zeile {min(loads)} gelesen, aber erst in Zeile "
        f"{min(stores)} zugewiesen — das ist ein UnboundLocalError, kein 400."
    )
