"""Utilities to display trees in the terminal (e.g., interactive using `curses`, via `rich`, or as JSON)"""

import functools
import json
import logging
import os
from abc import ABC
from typing import Any

from localstack_cli.utils.objects import SubtypesInstanceManager

# constants
ESC = 27
INDENTATION = 2


class TreeRenderer(SubtypesInstanceManager, ABC):
    """Abstract base class for rendering trees (e.g., cloud pod contents) on the terminal."""

    def render_tree(self, tree: dict[str, Any], tree_name: str):
        raise NotImplementedError


class TreeRendererRich(TreeRenderer):
    @staticmethod
    def impl_name() -> str:
        return "rich"

    def render_tree(self, tree: dict[str, Any], tree_name: str):
        from rich import print
        from rich.tree import Tree

        def _convert(obj, parent):
            if isinstance(obj, list):
                for idx, o in enumerate(obj):
                    subtree = Tree(str(idx))
                    parent.add(_convert(o, subtree))
                return parent
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        if not v:
                            return Tree(f"{k} = {v}")
                        subtree = Tree(str(k))
                        _convert(v, subtree)
                        parent.add(subtree)
                    else:
                        parent.add(Tree(f"{k} = {v}"))
                return parent
            return Tree(str(obj))

        tree_obj = Tree(tree_name)
        _convert(tree, tree_obj)
        print(tree_obj)


class Tree:
    def __init__(self, name, obj):
        self.name = name
        self.object = obj
        self.expanded = True

    def render(self, depth, width):
        padding = " " * INDENTATION * depth
        return self.pad(f"{padding}{self.icon()} {self.name}", width)

    @functools.cache
    def children(self):
        def _name(key, value):
            if isinstance(value, (list, dict)):
                return key
            return f"{value}" if isinstance(key, int) else f"{key} = {value}"

        if isinstance(self.object, dict):
            return [Tree(_name(k, v), v) for k, v in self.object.items()]
        if isinstance(self.object, list):
            return [Tree(_name(idx, v), v) for idx, v in enumerate(self.object)]
        return []

    def icon(self):
        if self.children() and not self.expanded:
            return "+"
        return "-"

    def expand(self):
        self.expanded = True

    def collapse(self):
        self.expanded = False

    def toggle(self):
        self.expanded = not self.expanded

    def traverse(self):
        yield self, 0
        if not self.expanded:
            return
        for _child in self.children():
            for child, depth in _child.traverse():
                yield child, depth + 1

    def pad(self, data, width):
        return data + " " * (width - len(data))


class TreeRendererCurses(TreeRenderer):
    """
    Renders an interactive tree in the terminal via the curses library.
    Loosely based on https://github.com/mcchae/treesel
    """

    # file logger, lazily initialized
    LOG = None

    @staticmethod
    def impl_name() -> str:
        return "curses"

    def render_tree(self, dict_obj: dict, tree_name: str):
        from curses import wrapper  # note: keep here to avoid import issues on some systems

        saved_fds = (os.dup(0), os.dup(1))

        def curses_main_wrapper(_tree: Tree):
            def _main(win):
                return self.curses_main(win, _tree)

            return _main

        try:
            saved_fds = self.open_tty()
            tree = Tree(tree_name, dict_obj)
            wrapper(curses_main_wrapper(tree))
        finally:
            os.close(0)
            os.close(1)
            os.dup(saved_fds[0])
            os.dup(saved_fds[1])

    @staticmethod
    def curses_main(win, tree: Tree):
        """
        Curses main loop that performs the rendering.
        :param win: the `curses.window` instance to render on
        :param tree: the tree structure to render
        """
        import curses  # note: keep here to avoid import issues on some systems

        # initialize window
        win.clear()
        win.refresh()
        curses.nl()
        curses.noecho()
        win.timeout(0)
        win.nodelay(False)
        tree.expand()
        selected_line = 3
        pending_action = None

        # set up default terminal colors
        curses.use_default_colors()

        # main render loop
        while True:
            win.clear()
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
            line = 0
            offset = max(0, selected_line - curses.LINES + 3)
            for data, depth in tree.traverse():
                if line == selected_line:
                    win.attrset(curses.color_pair(1) | curses.A_BOLD)
                    if pending_action:
                        getattr(data, pending_action)()
                        pending_action = None
                else:
                    win.attrset(curses.color_pair(0))
                if 0 <= line - offset < curses.LINES - 1:
                    win.addstr(line - offset, 0, data.render(depth, curses.COLS))
                line += 1
            win.refresh()
            ch = win.getch()
            if ch == curses.KEY_UP:
                selected_line -= 1
            elif ch == curses.KEY_DOWN:
                selected_line += 1
            elif ch == curses.KEY_PPAGE:
                selected_line -= curses.LINES
                if selected_line < 0:
                    selected_line = 0
            elif ch == curses.KEY_NPAGE:
                selected_line += curses.LINES
                if selected_line >= line:
                    selected_line = line - 1
            elif ch == curses.KEY_RIGHT:
                pending_action = "expand"
            elif ch == curses.KEY_LEFT:
                pending_action = "collapse"
            elif ch == ord(" "):
                pending_action = "toggle"
            elif ch == ESC:
                return

            selected_line %= line

    @staticmethod
    def open_tty():
        saved_stdin = os.dup(0)
        saved_stdout = os.dup(1)
        os.close(0)
        os.close(1)
        os.open("/dev/tty", os.O_RDONLY)
        os.open("/dev/tty", os.O_RDWR)
        return saved_stdin, saved_stdout

    @classmethod
    def log(cls, *args, **kwargs):
        """Logger that can be used to log debug output from curses program"""
        if cls.LOG:
            LOG = logging.getLogger(__file__)
            handler = logging.FileHandler("cloud_pods_viewer.log")
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            handler.setFormatter(formatter)
            LOG.addHandler(handler)
            LOG.setLevel(logging.INFO)
        cls.LOG.info(*args, **kwargs)


class TreeRendererJSON(TreeRenderer):
    @staticmethod
    def impl_name() -> str:
        return "json"

    def render_tree(self, dict_obj: dict, tree_name: str):
        # simply print out the JSON with indentation
        print(json.dumps(dict_obj, indent=4))
