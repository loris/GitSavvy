import re
from collections import namedtuple
from distutils.version import LooseVersion

from GitSavvy.core.git_command import mixin_base

TagDetails = namedtuple("TagDetails", ("sha", "tag", "human_date", "relative_date"))


MYPY = False
if MYPY:
    from typing import List, Optional, Tuple


class TagsMixin(mixin_base):

    def get_local_tags(self):
        # type: () -> Tuple[List[TagDetails], List[TagDetails]]
        stdout = self.git(
            "for-each-ref",
            "--sort=-creatordate",
            "--format={}".format(
                "%00".join((
                    "%(objectname)",
                    "%(refname:short)",
                    "%(creatordate:format:%e %b %Y)",
                    "%(creatordate:relative)",
                ))
            ),
            "refs/tags"
        )
        entries = [
            TagDetails(*line.split("\x00"))
            for line in stdout.splitlines()
            if line
        ]
        return self.handle_semver_tags(entries)

    def get_remote_tags(self, remote):
        # type: (str) -> Tuple[List[TagDetails], List[TagDetails]]
        stdout = self.git_throwing_silently(
            "ls-remote",
            "--tags",
            remote,
        )
        porcelain_entries = stdout.splitlines()
        entries = [
            TagDetails(entry[:40], entry[51:], "", "")
            for entry in reversed(porcelain_entries)
            if entry
        ]
        return self.handle_semver_tags(entries)

    def get_last_local_semver_tag(self):
        # type: () -> Optional[str]
        """
        Return the last tag of the current branch. get_tags() fails to return an ordered list.
        """
        _, tags = self.get_local_tags()
        return tags[0].tag if tags else ""

    def handle_semver_tags(self, entries):
        # type: (List[TagDetails]) -> Tuple[List[TagDetails], List[TagDetails]]
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
                # The error might me caused of having tags like 1.2.3.1 and 1.2.3.beta.
                # Exception thrown is "can't convert str to int" as it is comparing
                # 'beta' with 1.
                # Fallback and take only the numbers as sorting key.
                semver_entries = sorted(
                    semver_entries,
                    key=lambda entry: LooseVersion(
                        semver_test.search(entry.tag).group()  # type: ignore[union-attr]
                    ),
                    reverse=True
                )

        return (regular_entries, semver_entries)
