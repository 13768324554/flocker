# Copyright ClusterHQ Inc.  See LICENSE file for details.

import os

from twisted.python.filepath import FilePath
from ...testtools import (
    FlockerScriptTestsMixin, StandardOptionsTestsMixin, TestCase,
)
from .._script import CAScript, CAOptions


class FlockerCATests(FlockerScriptTestsMixin, TestCase):
    """
    Tests for ``flocker-ca`` CLI.
    """
    script = CAScript
    options = CAOptions
    command_name = u'flocker-ca'


class CAOptionsTests(StandardOptionsTestsMixin, TestCase):
    """
    Tests for :class:`CAOptions`.
    """
    options = CAOptions


class FlockerCAMainTests(TestCase):
    """
    Tests for ``CAScript.main``.
    """
    def test_deferred_result(self):
        """
        ``CAScript.main`` returns a ``Deferred`` on success.
        """
        # Ensure we don't conflict on buildbot with certificate
        # files already created in previous tests.
        path = FilePath(self.mktemp())
        path.makedirs()

        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(path.path)

        options = CAOptions()
        options.parseOptions(["initialize", "mycluster"])

        script = CAScript()
        dummy_reactor = object()

        self.assertEqual(
            None,
            self.successResultOf(script.main(dummy_reactor, options))
        )
