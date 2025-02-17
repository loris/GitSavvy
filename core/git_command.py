"""
Define a base command class that:
  1) provides a consistent interface with `git`,
  2) implements common git operations in one place, and
  3) tracks file- and repo- specific data the is necessary
     for Git operations.
"""

from collections import deque, ChainMap
import io
from itertools import chain, repeat
from functools import partial
import locale
import os
import re
import shutil
import stat
import subprocess
import time
import traceback

import sublime

from ..common import util
from .settings import SettingsMixin
from GitSavvy.core.fns import filter_
from GitSavvy.core.runtime import enqueue_on_worker, run_as_future
from GitSavvy.core.utils import paths_upwards, resolve_path


MYPY = False
if MYPY:
    from typing import Callable, Deque, Dict, Iterator, List, Optional, Sequence, Tuple, Union


git_path = None
error_message_displayed = False
repo_paths = {}  # type: Dict[str, str]
git_dirs = {}  # type: Dict[str, str]

DECODE_ERROR_MESSAGE = """
The Git command returned data that is unparsable.  This may happen
if you have checked binary data into your repository, or not UTF-8
encoded files.  In the latter case use the 'fallback_encoding' setting.

-- Partially decoded output follows; � denotes decoding errors --
"""

MIN_GIT_VERSION = (2, 18, 0)
GIT_TOO_OLD_MSG = "Your Git version is too old. GitSavvy requires {:d}.{:d}.{:d} or above."

NOT_SET = "<NOT_SET>"


def communicate_and_log(proc, stdin, log):
    # type: (subprocess.Popen, bytes, Callable[[bytes], None]) -> Tuple[bytes, bytes]
    """
    Emulates Popen.communicate
    Writes stdin (if provided)
    Logs output from both stdout and stderr
    Returns stdout, stderr
    """
    if stdin is not None:
        assert proc.stdin
        proc.stdin.write(stdin)
        proc.stdin.flush()
        proc.stdin.close()

    stdout, stderr = b'', b''
    for line in stream_stdout_and_err(proc):
        if isinstance(line, Out):
            stdout += line
            log(line)
        elif isinstance(line, Err):
            stderr += line
            log(line)

    return stdout, stderr


class Out(bytes): pass  # noqa: E701
class Err(bytes): pass  # noqa: E701


def read_linewise(fh, kont):
    # type: (io.BufferedReader, Callable[[bytes], None]) -> None
    for line in iter(fh.readline, b''):
        kont(line)


def stream_stdout_and_err(proc):
    # type: (subprocess.Popen) -> Iterator[bytes]
    container = deque()  # type: Deque[bytes]
    append = container.append
    out_f = run_as_future(read_linewise, proc.stdout, lambda line: append(Out(line)))
    err_f = run_as_future(read_linewise, proc.stderr, lambda line: append(Err(line)))
    delay = chain([1, 2, 4, 8, 15, 30], repeat(50))

    with proc:
        while out_f.running() or err_f.running():
            try:
                yield container.popleft()
            except IndexError:
                time.sleep(next(delay) / 1000)

    # Check and raise exceptions if any
    out_f.result()
    err_f.result()

    yield from container


STARTUPINFO = None
if os.name == "nt":
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW

HOME = os.path.expanduser('~')


def __search_for_git(folder):
    # type: (str) -> Optional[str]
    for p in paths_upwards(folder):
        if is_git_directory(os.path.join(p, ".git")):
            return p
        if p == HOME:
            break
    return None


def is_git_directory(suspect):
    # type: (str) -> bool
    try:
        st = os.stat(suspect)
    except (OSError, ValueError):
        return False

    if not stat.S_ISDIR(st.st_mode):
        return True
    # Test if the dir looks like a git dir.  `HEAD` is mandatory.
    ok = os.path.exists(os.path.join(suspect, "HEAD"))
    if not ok:
        util.debug.dprint("fatal: {} has no HEAD file.".format(suspect))
    return ok


def search_for_git(folder):
    # type: (str) -> Optional[str]
    util.debug.dprint("searching .git repo starting at ", folder)
    try:
        return __search_for_git(folder)
    except Exception as e:
        util.debug.dprint("searching raised: {}".format(e))
        return None


def search_for_git_toplevel(start_folder):
    # type: (str) -> Optional[str]
    real_start_folder = resolve_path(start_folder)
    real_repo_path = search_for_git(real_start_folder)
    if real_start_folder == start_folder:
        return real_repo_path
    if not real_repo_path:
        return None

    user_repo_path = search_for_git(start_folder)
    if user_repo_path and os.path.samefile(real_repo_path, user_repo_path):
        return user_repo_path
    return real_repo_path


def git_version_from_path(git_path):
    # type: (str) -> Optional[Tuple[int, ...]]
    try:
        stdout = subprocess.check_output(
            [git_path, "--version"],
            stderr=subprocess.PIPE,
            startupinfo=STARTUPINFO
        ).decode()
    except Exception:
        stdout = ""

    match = re.match(r"git version ([0-9]+)\.([0-9]+)\.([0-9]+)", stdout)
    if match:
        version = tuple(map(int, match.groups()))
        return version
    else:
        return None


def is_subpath(topfolder, path):
    # type: (str, str) -> bool
    return os.path.commonprefix([topfolder, path]) == topfolder


class _GitCommand(SettingsMixin):

    """
    Base class for all Sublime commands that interact with git.
    """

    def git(
        self,
        git_cmd,
        *args,  # type: Optional[str]
        stdin=None,
        working_dir=None,
        show_panel=None,
        show_panel_on_error=True,
        throw_on_error=True,
        decode=True,
        stdin_encoding="utf-8",
        custom_environ=None,
        just_the_proc=False
    ):
        """
        Run the git command specified in `*args` and return the output
        of the git command as a string.

        If stdin is provided, it should be a string and will be piped to
        the git process.  If `working_dir` is provided, set this as the
        current working directory for the git process; otherwise,
        the `repo_path` value will be used.
        """
        window = self.some_window()
        final_args = self._add_global_flags(git_cmd, list(args))
        command = [self.git_binary_path] + list(filter_(final_args))
        command_str = util.debug.pretty_git_command(command[1:])

        if show_panel is None:
            show_panel = git_cmd in self.savvy_settings.get("show_panel_for")

        if show_panel:
            panel = util.log.init_panel(window)
            log = partial(util.log.append_to_panel, panel)
            log("$ {}\n".format(command_str))

        if not working_dir:
            try:
                working_dir = self.repo_path
            except RuntimeError as e:
                # do not show panel when the window does not exist
                raise GitSavvyError(str(e), show_panel=False, window=window)
            except Exception as e:
                raise GitSavvyError(str(e), show_panel=show_panel_on_error, window=window)

        stdout, stderr = None, None
        environ = ChainMap(
            custom_environ or {},
            self.savvy_settings.get("env") or {},
            os.environ
        )
        try:
            start = time.time()
            p = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=working_dir,
                env=environ,
                startupinfo=STARTUPINFO
            )

            if just_the_proc:
                return p

            if isinstance(stdin, str):
                stdin = stdin.encode(encoding=stdin_encoding)

            if show_panel:
                log_b = lambda line: log(line.decode("utf-8", "replace"))
                stdout, stderr = communicate_and_log(p, stdin, log_b)
            else:
                stdout, stderr = p.communicate(stdin)

        except Exception as e:
            # this should never be reached
            raise GitSavvyError(
                "$ {} ({})\n\n"
                "Please report this error to GitSavvy:\n\n{}\n\n{}".format(
                    command_str, working_dir, e, traceback.format_exc()
                ),
                cmd=command,
                show_panel=show_panel_on_error,
                window=window
            )

        finally:
            if not just_the_proc:
                end = time.time()
                util.debug.log_git(final_args, working_dir, stdin, stdout, stderr, end - start)
                if show_panel:
                    log("\n[Done in {:.2f}s]".format(end - start))

        if decode:
            try:
                stdout, stderr = self.strict_decode(stdout), self.strict_decode(stderr)  # type: ignore[assignment]
            except UnicodeDecodeError:
                stdout_s = stdout.decode("utf-8", "replace")
                stderr_s = stderr.decode("utf-8", "replace")
                raise GitSavvyError(
                    "$ {}\n{}{}{}".format(
                        command_str,
                        DECODE_ERROR_MESSAGE,
                        stdout_s,
                        stderr_s,
                    ),
                    cmd=command,
                    stdout=stdout_s,
                    stderr=stderr_s,
                    show_panel=show_panel_on_error,
                    window=window
                )

        if throw_on_error and not p.returncode == 0:
            stdout_s, stderr_s = self.ensure_decoded(stdout), self.ensure_decoded(stderr)
            if "*** Please tell me who you are." in stderr_s:
                show_panel_on_error = False
                sublime.set_timeout_async(
                    lambda: sublime.active_window().run_command("gs_setup_user"))

            raise GitSavvyError(
                "$ {}\n\n{}{}".format(
                    command_str,
                    stdout_s,
                    (
                        "<no output, exit code: {}>".format(p.returncode)
                        if not stdout_s and not stderr_s else
                        stderr_s
                    )
                ),
                cmd=command,
                stdout=stdout_s,
                stderr=stderr_s,
                # If `show_panel` is set, we log *while* running the process
                # and thus don't need to log again.
                show_panel=show_panel_on_error and not show_panel,
                window=window
            )

        return stdout

    def git_throwing_silently(self, *args, **kwargs):
        return self.git(
            *args,
            throw_on_error=True,
            show_panel_on_error=False,
            **kwargs
        )

    def get_encoding_candidates(self):
        # type: () -> Sequence[str]
        return [
            'utf-8',
            locale.getpreferredencoding(),
            self.savvy_settings.get("fallback_encoding")
        ]

    def strict_decode(self, input):
        # type: (bytes) -> str
        encodings = self.get_encoding_candidates()
        decoded, _ = self.try_decode(input, encodings)
        return decoded

    def ensure_decoded(self, input):
        # type: (Union[str, bytes]) -> str
        if isinstance(input, str):
            return input
        return self.lax_decode(input)

    def lax_decode(self, input):
        # type: (bytes) -> str
        try:
            return self.strict_decode(input)
        except UnicodeDecodeError:
            return input.decode("utf-8", "replace")

    def try_decode(self, input, encodings):
        # type: (bytes, Sequence[str]) -> Tuple[str, str]
        for n, encoding in enumerate(encodings, start=1):
            try:
                return input.decode(encoding), encoding
            except UnicodeDecodeError as err:
                if n == len(encodings):
                    raise err
        assert False  # no silent fall-through

    @property
    def git_binary_path(self):
        """
        Return the path to the available `git` binary.
        """

        global git_path, error_message_displayed
        if not git_path:
            git_path_setting = self.savvy_settings.get("git_path")
            if isinstance(git_path_setting, dict):
                git_path = git_path_setting.get(sublime.platform())
                if not git_path:
                    git_path = git_path_setting.get('default')
            else:
                git_path = git_path_setting

            if not git_path:
                git_path = shutil.which("git")

            if git_path:
                util.debug.dprint("git executable: {}".format(git_path))
                version = git_version_from_path(git_path)
                if version:
                    util.debug.dprint("git version: {}".format(version))
                    if version < MIN_GIT_VERSION:
                        msg = GIT_TOO_OLD_MSG.format(*MIN_GIT_VERSION)
                        git_path = None
                        if not error_message_displayed:
                            sublime.error_message(msg)
                            error_message_displayed = True
                        raise ValueError("Git binary too old.")
                else:
                    git_path = None

        if not git_path:
            msg = ("Your Git binary cannot be found.  If it is installed, add it "
                   "to your PATH environment variable, or add a `git_path` setting "
                   "in the GitSavvy settings.")
            if not error_message_displayed:
                sublime.error_message(msg)
                error_message_displayed = True
            raise ValueError("Git binary not found.")

        return git_path

    def _current_window(self):
        # type: () -> Optional[sublime.Window]
        try:
            return self.window  # type: ignore[attr-defined]
        except AttributeError:
            return self.view.window()  # type: ignore[attr-defined]

    def _current_view(self):
        # type: () -> Optional[sublime.View]
        try:
            return self.view  # type: ignore[attr-defined]
        except AttributeError:
            return self.window.active_view()  # type: ignore[attr-defined]

    def _current_filename(self):
        # type: () -> Optional[str]
        try:
            return self.view.file_name()  # type: ignore[attr-defined]
        except AttributeError:
            return self.window.extract_variables().get("file")  # type: ignore[attr-defined]

    def _search_paths(self):
        # type: () -> Iterator[str]
        def __search_paths():
            # type: () -> Iterator[str]
            file_name = self._current_filename()
            if file_name and not os.path.isfile(file_name):
                file_name = None
            if file_name:
                yield os.path.dirname(file_name)

            window = self._current_window()
            if window:
                folders = window.folders()
                if folders:
                    if (
                        not file_name
                        or not is_subpath(resolve_path(folders[0]), resolve_path(file_name))
                    ):
                        yield folders[0]

        return filter(os.path.isdir, __search_paths())

    def find_repo_path(self):
        # type: () -> Optional[str]
        view = self._current_view()
        repo_path = view.settings().get("git_savvy.repo_path") if view else None
        if repo_path and os.path.exists(repo_path):
            return repo_path

        return next(filter_(map(self._find_git_toplevel, self._search_paths())), None)

    def _find_git_toplevel(self, folder):
        # type: (str) -> Optional[str]
        try:
            return repo_paths[folder]
        except KeyError:
            repo_path = search_for_git_toplevel(folder)
            if repo_path:
                util.debug.dprint("repo path:", os.path.join(repo_path, ".git"))
                repo_paths[folder] = repo_path
            else:
                util.debug.dprint("found no .git path for {}".format(folder))
            return repo_path

    def get_repo_path(self):
        # type: () -> str
        repo_path = self.find_repo_path()
        if repo_path:
            return repo_path

        window = self._current_window()
        if not window:
            raise RuntimeError("Window does not exist.")

        if window.folders():
            enqueue_on_worker(window.run_command, "gs_offer_init")
        raise ValueError("Not a git repository.")

    @property
    def repo_path(self):
        # type: () -> str
        """
        Return the absolute path to the git repo that contains the file that this
        view interacts with.  Like `file_path`, this can be overridden by setting
        the view's `git_savvy.repo_path` setting.
        """
        return self.get_repo_path()

    @property
    def short_repo_path(self):
        if "HOME" in os.environ:
            return self.repo_path.replace(os.environ["HOME"], "~")
        else:
            return self.repo_path

    @property
    def git_dir(self):
        # type: () -> str
        repo_path = self.repo_path
        try:
            return git_dirs[repo_path]
        except KeyError:
            # Note: per contract `{self.repo_path}/.git` exists.
            gitdir = os.path.join(repo_path, ".git")
            if os.path.isfile(gitdir):
                try:
                    with open(gitdir) as f:
                        content = f.read()
                        if content.startswith("gitdir: "):
                            gitdir = content[8:].strip()
                except OSError:
                    pass
            git_dirs[repo_path] = gitdir
            return gitdir

    @property
    def file_path(self):
        # type: () -> Optional[str]
        """
        Return the absolute path to the file this view interacts with. In most
        cases, this will be the open file.  However, for views with special
        functionality, this default behavior can be overridden by setting the
        view's `git_savvy.file_path` setting.
        """
        view = self._current_view()
        if not view:
            return None

        return view.settings().get("git_savvy.file_path") or view.file_name()

    def get_rel_path(self, abs_path=NOT_SET):
        # type: (str) -> str
        """
        Return the file path relative to the repo root.
        """
        fpath = self.file_path if abs_path is NOT_SET else abs_path
        assert fpath
        rel_path = os.path.relpath(resolve_path(fpath), start=resolve_path(self.repo_path))
        if os.name == "nt":
            return rel_path.replace("\\", "/")
        return rel_path

    def _add_global_flags(self, git_cmd, args):
        # type: (str, List[Optional[str]]) -> List[str]
        """
        Transforms the Git command arguments with flags indicated in the
        global GitSavvy settings.
        """
        global_pre_flags = self.savvy_settings.get("global_pre_flags", {}).get(git_cmd, [])
        global_flags = self.savvy_settings.get("global_flags", {}).get(git_cmd, [])
        return global_pre_flags + [git_cmd] + global_flags + args


if MYPY:
    mixin_base = _GitCommand
else:
    mixin_base = object


from .git_mixins.status import StatusMixin  # noqa: E402
from .git_mixins.active_branch import ActiveBranchMixin  # noqa: E402
from .git_mixins.branches import BranchesMixin  # noqa: E402
from .git_mixins.stash import StashMixin  # noqa: E402
from .git_mixins.stage_unstage import StageUnstageMixin  # noqa: E402
from .git_mixins.checkout_discard import CheckoutDiscardMixin  # noqa: E402
from .git_mixins.remotes import RemotesMixin  # noqa: E402
from .git_mixins.ignore import IgnoreMixin  # noqa: E402
from .git_mixins.tags import TagsMixin  # noqa: E402
from .git_mixins.history import HistoryMixin  # noqa: E402
from .git_mixins.rewrite import RewriteMixin  # noqa: E402
from .git_mixins.merge import MergeMixin  # noqa: E402
from .exceptions import GitSavvyError  # noqa: E402


class GitCommand(
    RewriteMixin,
    ActiveBranchMixin,

    RemotesMixin,  # depends on BranchesMixin
    BranchesMixin,
    CheckoutDiscardMixin,

    StatusMixin,  # depends on HistoryMixin
    HistoryMixin,

    IgnoreMixin,
    MergeMixin,
    StageUnstageMixin,
    StashMixin,
    TagsMixin,
    _GitCommand
):
    pass
