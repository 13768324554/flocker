# Copyright ClusterHQ Inc.  See LICENSE file for details.
"""
Tests for ``admin.installer``.
"""
import json
import os
from subprocess import check_call, CalledProcessError
from unittest import skipIf

import boto3
from botocore.vendored.requests.packages.urllib3.contrib.pyopenssl import (
    extract_from_urllib3,
)

from effect import Effect, sync_perform
from effect.testing import perform_sequence

from txeffect import perform as async_perform

from pyrsistent import PClass, field, pmap_field, thaw

from testtools.matchers import StartsWith
from testtools.content import text_content, content_from_file, ContentType
from testtools.matchers import Contains

from twisted.internet.error import ProcessTerminated
from twisted.python.filepath import FilePath

from flocker.testtools import (
    AsyncTestCase, TestCase, random_name, FakeSysModule
)

from ..installer._images import (
    StandardOut, PackerBuild,
    _PackerOutputParser, PackerConfigure,
    AWS_REGIONS, publish_installer_images_effects, RealPerformers,
    PublishInstallerImagesOptions, PACKER_PATH
)

# Don't use pyOpenSSL in urllib3 - it causes an ``OpenSSL.SSL.Error``
# exception when we try an API call on an idled persistent connection.
# See https://github.com/boto/boto3/issues/220
extract_from_urllib3()

try:
    import flocker as _flocker
    REPOSITORY = FilePath(_flocker.__file__).parent().parent()
finally:
    del _flocker

PACKER_OUTPUTS = FilePath(__file__).sibling('packer_outputs')

require_packer = skipIf(
    not PACKER_PATH.exists(),
    "Tests require ``packer`` to be installed at ``/opt/packer/packer``."
)


def default_options():
    """
    :return: The default ``PublishInstallerOptions``.
    """
    options = PublishInstallerImagesOptions()
    options.parseOptions([])
    return options


class ParserData(PClass):
    """
    A record to store sample input and output which can be used to test the
    PackerOutputParser.

    ;ivar FilePath input: A file containing sample ``packer build
         -machine-readable`` output which will be fed into the parser.
    :ivar pmap output: The expected dictionary of the regional AMI values after
        parsing ``input``.
    """
    input = field(type=FilePath, mandatory=True)
    output = pmap_field(key_type=unicode, value_type=unicode, optional=False)


# These are sample outputs of running ``packer build -machine-readable`` with
# configuration files which result in Packer publishing AMI images to multiple
# or one Amazon region.
PACKER_OUTPUT_US_ALL = ParserData(
    input=PACKER_OUTPUTS.child('PACKER_OUTPUT_US_ALL'),
    output={
        u"us-east-1": u"ami-dc4410b6",
        u"us-west-1": u"ami-e098f380",
        u"us-west-2": u"ami-8c8f90ed",
    }
)

PACKER_OUTPUT_US_WEST_1 = ParserData(
    input=PACKER_OUTPUTS.child('PACKER_OUTPUT_US_WEST_1'),
    output={u"us-west-1": u"ami-e098f380"}
)

# This is an example of running ``packer build -machine-readble`` with a
# configuration file that has no builders.
PACKER_OUTPUT_NONE = ParserData(
    input=PACKER_OUTPUTS.child('PACKER_OUTPUT_NONE'),
    output={},
)


class PackerOutputParserTests(TestCase):
    """
    Tests for ``_PackerOutputParser``.
    """
    def test_artifact(self):
        """
        An artifact is recorded when the first ``end`` parameter is
        encountered.
        """
        parser = _PackerOutputParser()
        parser.parse_line(
            '1450420216,amazon-ebs,artifact,0,builder-id,mitchellh.amazonebs\n'
        )
        parser.parse_line('1450420216,amazon-ebs,artifact,0,end\n')
        self.assertEqual(
            [{u'type': u'amazon-ebs',
              u'builder-id': u'mitchellh.amazonebs'}],
            parser.artifacts
        )

    def test_artifact_multiple(self):
        """
        An artifact is appended when another ``end`` parameter is encountered.
        """
        parser = _PackerOutputParser()
        parser.parse_line('1450420216,amazon-ebs,artifact,0,end\n')
        parser.parse_line('1450420216,foobar,artifact,0,end\n')
        self.assertEqual(
            [{'type': 'amazon-ebs'},
             {'type': 'foobar'}],
            parser.artifacts
        )


class PackerAmisTests(TestCase):
    """
    Tests for ``PackerOutputParser.packer_amis``.
    """
    def assert_packer_amis(self, parser_data):
        """
        Assert that parser input produces the expected AMI artifacts.

        :param ParserData parser_data: The input and and expected AMI map.
        """
        parser = _PackerOutputParser()
        with parser_data.input.open('r') as f:
            for line in f:
                parser.parse_line(line)
        self.assertEqual(parser_data.output, parser.packer_amis())

    def test_no_ami(self):
        """
        If there are no AMI artifacts, the return value is an empty dictionary.
        """
        self.assert_packer_amis(PACKER_OUTPUT_NONE)

    def test_one_ami(self):
        """
        If there is a single AMI artifact, the return value is a single item
        dictionary.
        """
        self.assert_packer_amis(PACKER_OUTPUT_US_WEST_1)

    def test_multiple_ami(self):
        """
        If there are multiple AMI artifacts, the return value is a multiple
        item dictionary.
        """
        self.assert_packer_amis(PACKER_OUTPUT_US_ALL)


class PackerConfigureTests(TestCase):
    """
    Tests for ``PackerConfigure``.
    """
    def test_configuration(self):
        """
        Source AMI ID, build region, and target regions can all be overridden
        in a chosen template.
        """
        expected_build_region = AWS_REGIONS.EU_WEST_1
        expected_publish_regions = [
            AWS_REGIONS.AP_NORTHEAST_1,
            AWS_REGIONS.AP_SOUTHEAST_1,
            AWS_REGIONS.AP_SOUTHEAST_2,
        ]
        expected_source_ami = random_name(self)
        intent = PackerConfigure(
            build_region=expected_build_region,
            publish_regions=expected_publish_regions,
            source_ami=expected_source_ami,
            template=u"docker",
            distribution=u"ubuntu-14.04",
        )

        # Call the performer
        packer_configuration_path = sync_perform(
            dispatcher=RealPerformers(
                working_directory=self.make_temporary_directory()
            ).dispatcher(),
            effect=Effect(intent=intent)
        )
        with packer_configuration_path.open('r') as f:
            packer_configuration = json.load(f)

        [builder] = packer_configuration["builders"]
        build_region = builder['region']
        build_source_ami = builder['source_ami']
        publish_regions = builder['ami_regions']
        [provisioner] = packer_configuration["provisioners"]
        self.assertEqual(
            (expected_build_region.value,
             set(c.value for c in expected_publish_regions),
             expected_source_ami),
            (build_region, set(publish_regions),
             build_source_ami)
        )


class PackerBuildIntegrationTests(AsyncTestCase):
    """
    Integration tests for ``PackerBuild``.
    """
    @require_packer
    def test_template_error(self):
        """
        Template errors result in the process exiting and an error message
        printed to stderr.
        Packer prints machine-readable output to stderr and
        ``publish-installer-images`` echos those lines to its stderr as well as
        parsing the output.
        """
        sys_module = FakeSysModule()
        self.addCleanup(
            lambda: self.addDetail(
                name="stderr",
                content_object=text_content(
                    sys_module.stderr.getvalue()
                )
            )
        )

        configuration_path = self.make_temporary_file(content='')

        d = async_perform(
            dispatcher=RealPerformers(
                sys_module=sys_module,
            ).dispatcher(),
            effect=Effect(
                intent=PackerBuild(
                    configuration_path=configuration_path,
                )
            )
        )

        d = self.assertFailure(d, ProcessTerminated)

        def check_error(exception):
            self.assertEqual(1, exception.exitCode)
            self.assertIn(
                "Failed to parse template", sys_module.stderr.getvalue()
            )
        return d.addCallback(check_error)


class StandardOutTests(TestCase):
    """
    Tests for ``StandardOut``.
    """
    def test_perform(self):
        """
        ``StandardOut`` has a performer that writes content to sys.stdout.
        """
        fake_sys_module = FakeSysModule()
        intent = StandardOut(
            content=random_name(self).encode('ascii'),
        )
        result = sync_perform(
            dispatcher=RealPerformers(
                sys_module=fake_sys_module
            ).dispatcher(),
            effect=Effect(intent=intent)
        )
        self.assertIs(None, result)
        self.assertEqual(
            intent.content,
            fake_sys_module.stdout.getvalue()
        )


class PublishInstallerImagesEffectsTests(TestCase):
    """
    Tests for ``publish_installer_images_effects``
    """
    def test_sequence(self):
        """
        The function generates a packer configuration file, runs packer
        build and uploads the AMI ids to a given S3 bucket.
        """
        options = default_options()
        configuration_path = self.make_temporary_directory()
        ami_map = PACKER_OUTPUT_US_ALL.output
        perform_sequence(
            seq=[
                (PackerConfigure(
                    build_region=options["build_region"],
                    publish_regions=options["regions"],
                    source_ami=options["source_ami"],
                    template=options["template"],
                    distribution=options["distribution"],
                ), lambda intent: configuration_path),
                (PackerBuild(
                    configuration_path=configuration_path,
                ), lambda intent: ami_map),
                (StandardOut(
                    content=json.dumps(
                        thaw(ami_map),
                        encoding='utf-8',
                    ),
                ), lambda intent: None),
            ],
            eff=publish_installer_images_effects(options=options)
        )


class PublishInstallerImagesIntegrationTests(TestCase):
    """
    Integration test for ``publish-installer-images``.
    """
    script = REPOSITORY.descendant(['admin', 'publish-installer-images'])

    def publish_installer_images(self, args, expect_error=False,
                                 extra_enviroment=None):
        """
        Call ``publish-installer-images`` capturing stdout and stderr.
        """
        working_directory = self.make_temporary_directory()
        environment = os.environ.copy()
        if extra_enviroment is not None:
            environment.update(extra_enviroment)
        # XXX Don't use TMPDIR because it breaks packer
        # https://github.com/mitchellh/packer/issues/2792
        environment["TEMP"] = working_directory.path
        stdout_path = working_directory.child('stdout')
        stderr_path = working_directory.child('stderr')
        self.addDetail(
            'stdout {!r}'.format(args),
            content_from_file(
                stdout_path.path,
                ContentType('text', 'plain')
            )
        )
        self.addDetail(
            'stderr {!r}'.format(args),
            content_from_file(
                stderr_path.path,
                ContentType('text', 'plain')
            )
        )

        with stdout_path.open('w') as stdout:
            with stderr_path.open('w') as stderr:
                try:
                    return_code = check_call(
                        [self.script.path] + args,
                        env=environment,
                        stdout=stdout, stderr=stderr
                    )
                except CalledProcessError as e:
                    self.addDetail(
                        'CalledProcessError {!r}'.format(args),
                        text_content(str(e))
                    )

                    if expect_error:
                        return_code = e.returncode
                    else:
                        raise

        return (return_code, stdout_path, stderr_path,)

    def test_help(self):
        """
        ``publish-installer-images`` has a ``--help`` option and includes the
        name of the script in its usage message.
        """
        returncode, stdout, stderr = self.publish_installer_images(
            args=["--help"]
        )
        self.expectThat(
            stdout.getContent(),
            StartsWith(u"Usage: {}".format(self.script.basename()))
        )

    @require_packer
    def foo_test_build_both(self):
        """
        ``publish-installer-images`` can be called twice to first generate
        Docker images and then Flocker images built on the Docker images.
        The IDs of the generated AMIs are published to S3.
        """
        docker_version = u"1.10.0"
        swarm_version = u"1.0.1"
        flocker_version = u"1.10.1"
        build_region = u"us-west-1"
        returncode, stdout, stderr = self.publish_installer_images(
            args=['--template', 'docker',
                  '--copy_to_all_regions',
                  '--build_region', build_region],
            extra_enviroment={
                u'DOCKER_VERSION': docker_version,
                u'SWARM_VERSION': swarm_version,
            }
        )
        self.addDetail(
            'docker_object_content', text_content(stdout)
        )

        # It should be valid JSON.
        docker_ami_map = json.loads(stdout)

        # We can use that image as the source for the Flocker image
        returncode, stdout, stderr = self.publish_installer_images(
            args=['--template', 'flocker',
                  '--build_region', build_region,
                  '--copy_to_all_regions',
                  '--source_ami', docker_ami_map[build_region]],
            extra_enviroment={
                u'FLOCKER_VERSION': flocker_version
            }
        )
        self.addDetail(
            'flocker_object_content', text_content(stdout)
        )
        # It should be valid JSON.
        flocker_ami_map = json.loads(stdout)

        ec2 = boto3.resource('ec2', region_name=build_region)
        # Get a list of all the related images and tags in case of errors.
        all_images = dict(
            (i.id, dict(name=i.name, tags=i.tags))
            for i in ec2.images.filter(
                Owners=[u'self'],
                Filters=[
                    {
                        u'Name': 'name',
                        u'Values': [
                            u'clusterhq_ubuntu-14.04_docker*',
                            u'clusterhq_ubuntu-14.04_flocker*'
                        ]
                    }
                ]
            )
        )
        self.addDetail(
            u"all_images",
            text_content(json.dumps(all_images))
        )

        docker_image = ec2.Image(docker_ami_map[build_region])
        self.expectThat(
            docker_image.tags,
            Contains({u'Key': 'DOCKER_VERSION', u'Value': docker_version}),
        )
        self.expectThat(
            docker_image.tags,
            Contains({u'Key': 'SWARM_VERSION', u'Value': swarm_version}),
        )

        flocker_image = ec2.Image(flocker_ami_map[build_region])
        self.expectThat(
            flocker_image.tags,
            Contains({u'Key': 'FLOCKER_VERSION', u'Value': flocker_version}),
        )
