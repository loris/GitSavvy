from contextlib import contextmanager
from functools import lru_cache
import inspect
import threading

import sublime
import sublime_plugin

from GitSavvy.core.git_command import GitCommand
from GitSavvy.core.ui_mixins.quick_panel import show_branch_panel


MYPY = False
if MYPY:
    from typing import Any, Callable, Dict, Iterator, List, Protocol, TypeVar
    CommandT = TypeVar("CommandT", bound=sublime_plugin.Command)
    Args = Dict[str, Any]

    class Kont(Protocol):
        def __call__(self, val: object, **kw: object) -> None:
            pass

    ArgProvider = Callable[[CommandT, Args, Kont], None]


class WithProvideWindow:
    window = None  # type: sublime.Window

    def run_(self, edit_token, args):
        window = self.view.window()  # type: ignore[attr-defined]
        if not window:
            return
        # Very difficult to tell when Sublime actually instantiates
        # new objects (selfs).  However, for some commands a TextCommand
        # instance is long-lived.  Since we usually defer to the worker
        # or some other thread **and** want to use `self` as the current
        # context object, we clone manually here.
        # So we get a new context per call which also means we can't store
        # state on `self` to be available on the next call.
        cloned = self.__class__(self.view)  # type: ignore[attr-defined, call-arg]
        cloned.window = window
        return super(WithProvideWindow, cloned).run_(edit_token, args)  # type: ignore[misc]


class WithInputHandlers:
    defaults = {}  # type: Dict[str, ArgProvider]

    def run_(self, edit_token, args):
        if not self.defaults:
            return super().run_(edit_token, args)  # type: ignore[misc]

        args = self.filter_args(args)  # type: ignore[attr-defined]
        if args is None:
            args = {}

        present = args.keys()
        for name in ordered_positional_args(self.run):  # type: ignore[attr-defined]
            if name not in present and name in self.defaults:
                sync_mode = Flag()
                done = make_on_done_fn(
                    lambda: (
                        None
                        if sync_mode
                        else run_command(self, args)  # type: ignore[arg-type]
                    ),
                    args,
                    name
                )
                with sync_mode.set():
                    self.defaults[name](self, args, done)
                if not done.called:
                    break
        else:
            return super().run_(edit_token, args)  # type: ignore[misc]


@lru_cache()
def ordered_positional_args(fn):
    # type: (Callable) -> List[str]
    return [
        name
        for name, parameter in inspect.signature(fn).parameters.items()
        if parameter.default is inspect.Parameter.empty
    ]


class Flag:
    def __init__(self):
        self._event = threading.Event()

    @contextmanager
    def set(self):
        # type: () -> Iterator[None]
        self._event.set()
        try:
            yield
        finally:
            self._event.clear()

    def __bool__(self):
        # type: () -> bool
        return self._event.is_set()


def make_on_done_fn(kont, args, name):
    def on_done(value, **kwargs):
        on_done.called = True  # type: ignore[attr-defined]
        args[name] = value
        args.update(kwargs)
        kont()
    on_done.called = False  # type: ignore[attr-defined]
    return on_done


def run_command(cmd, args):
    # type: (sublime_plugin.Command, Args) -> None
    _get_run_command(cmd)(cmd.name(), args)


def _get_run_command(cmd):
    # type: (sublime_plugin.Command) -> Callable[[str, Dict], None]
    if isinstance(cmd, sublime_plugin.TextCommand):
        return cmd.view.run_command
    elif isinstance(cmd, sublime_plugin.WindowCommand):
        return cmd.window.run_command
    else:
        return sublime.run_command


class GsTextCommand(
    WithInputHandlers,
    WithProvideWindow,
    sublime_plugin.TextCommand,
    GitCommand,
):
    defaults = {}  # type: Dict[str, Callable[[GsTextCommand, Args, Kont], None]]


class GsWindowCommand(
    WithInputHandlers,
    sublime_plugin.WindowCommand,
    GitCommand,
):
    defaults = {}  # type: Dict[str, Callable[[GsWindowCommand, Args, Kont], None]]


if MYPY:
    from typing import Union
    GsCommand = Union[GsTextCommand, GsWindowCommand]


# COMMON INPUT HANDLERS


def ask_for_branch(**kw):
    # type: (...) -> ArgProvider
    def handler(self, args, done):
        # type: (GsCommand, Args, Kont) -> None
        show_branch_panel(done, **kw)

    return handler


ask_for_local_branch = ask_for_branch(
    ignore_current_branch=True,
    local_branches_only=True
)
