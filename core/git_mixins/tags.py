import re
from collections import namedtuple
from distutils.version import LooseVersion

from GitSavvy.core.git_command import mixin_base

TagDetails = namedtuple("TagDetails", ("sha", "tag"))


class TagsMixin(mixin_base):

    def get_tags(self, remote=None):
        """
        Return a list of TagDetails object. These objects correspond
        to all tags found in the repository, containing abbreviated
        hashes and reference names.
        """
        stdout = self.git(
            "ls-remote" if remote else "show-ref",
            "--tags",
            remote if remote else None,
            throw_on_error=False
        )
        porcelain_entries = stdout.splitlines()
        entries = [
            TagDetails(entry[:40], entry[51:])
            for entry in reversed(porcelain_entries)
            if entry
        ]
        entries = self.handle_semver_tags(entries)

        return entries

    def get_last_local_tag(self):
        """
        Return the last tag of the current branch. get_tags() fails to return an ordered list.
        """

        tag = self.git("describe", "--tags", "--abbrev=0", throw_on_error=False).strip()
        return tag

    def handle_semver_tags(self, entries):
        """
        Sorts tags using LooseVersion if there's a tag matching the semver format.
        """

        semver_test = re.compile(r'\d+\.\d+\.?\d*')

        semver_entries, regular_entries = [], []
        for entry in entries:
            if semver_test.search(entry.tag):
                semver_entries.append(entry)
            else:
                regular_entries.append(entry)
        if len(semver_entries):
            try:
                semver_entries = sorted(
                    semver_entries,
                    key=lambda entry: LooseVersion(entry.tag),
                    reverse=True
                )
            except Exception:
                # the error might me caused of having tags like 1.2.3.1, 1.2.3.beta
                # exception is cant convert str to int, it is comparing 'beta' to 1
                # if that fails then only take the numbers and sort them
                semver_entries = sorted(
                    semver_entries,
                    key=lambda entry: LooseVersion(
                        semver_test.search(entry.tag).group()  # type: ignore[union-attr]
                    ),
                    reverse=True)

        return semver_entries + regular_entries
