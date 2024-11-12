import dataclasses
import os
from typing import TYPE_CHECKING, Optional, cast

import tmt
import tmt.base
import tmt.hardware
import tmt.log
import tmt.steps
import tmt.steps.provision
import tmt.steps.provision.testcloud
import tmt.utils
from tmt.steps.provision.testcloud import GuestTestcloud
from tmt.utils import field
from tmt.utils.templates import render_template

if TYPE_CHECKING:
    from tmt.hardware import Size

DEFAULT_TMP_PATH = "/var/tmp/tmt"  # noqa: S108
CONTAINER_STORAGE_DIR = tmt.utils.Path("/var/lib/containers/storage")

PODMAN_MACHINE_NAME = 'podman-machine-tmt'
PODMAN_ENV = tmt.utils.Environment.from_dict(
    {"CONTAINER_CONNECTION": f'{PODMAN_MACHINE_NAME}-root'})

DEFAULT_PODMAN_MACHINE_CPU = 2
DEFAULT_PODMAN_MACHINE_MEM: 'Size' = tmt.hardware.UNITS('2048 MB')
DEFAULT_PODMAN_MACHINE_DISK_SIZE: 'Size' = tmt.hardware.UNITS('50 GB')

CONTAINER_TEMPLATE = """
FROM {{ base_image }}

RUN <<EOF
set -euo pipefail

# install dependencies required by tmt
dnf -y install cloud-init rsync
ln -s ../cloud-init.target /usr/lib/systemd/system/default.target.wants
dnf clean all

# add the scripts in /var/lib/tmt/scripts to the PATH
touch /etc/environment
echo "export PATH=$PATH:/var/lib/tmt/scripts" >> /etc/environment

EOF
"""


class GuestBootc(GuestTestcloud):
    containerimage: str
    _rootless: bool

    def __init__(self,
                 *,
                 data: tmt.steps.provision.GuestData,
                 name: Optional[str] = None,
                 parent: Optional[tmt.utils.Common] = None,
                 logger: tmt.log.Logger,
                 containerimage: str,
                 rootless: bool) -> None:
        super().__init__(data=data, logger=logger, parent=parent, name=name)
        self.containerimage = containerimage
        self._rootless = rootless

    def remove(self) -> None:
        tmt.utils.Command(
            "podman",
            "rmi",
            self.containerimage).run(
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger,
            env=PODMAN_ENV if self._rootless else None)

        try:
            tmt.utils.Command(
                "podman", "machine", "rm", "-f", PODMAN_MACHINE_NAME
                ).run(cwd=self.workdir, stream_output=True, logger=self._logger)
        except BaseException:
            self._logger.debug(
                "Unable to remove podman machine {PODMAN_MACHINE_NAME}, it might not exist")

        super().remove()


@dataclasses.dataclass
class BootcData(tmt.steps.provision.testcloud.ProvisionTestcloudData):
    containerfile: Optional[str] = field(
        default=None,
        option='--containerfile',
        metavar='CONTAINERFILE',
        help="""
             Select container file to be used to build a container image
             that is then used by bootc install to create a disk image.

             Cannot be used with containerimage.
             """)

    containerfile_workdir: str = field(
        default=".",
        option=('--containerfile-workdir'),
        metavar='CONTAINERFILE_WORKDIR',
        help="""
             Select working directory for the podman build invocation.
             """)

    containerimage: Optional[str] = field(
        default=None,
        option=('--containerimage'),
        metavar='CONTAINERIMAGE',
        help="""
             Select container image to be used to build a bootc disk.
             This takes priority over containerfile.
             """)

    add_deps: bool = field(
        default=True,
        is_flag=True,
        option=('--add-deps'),
        help="""
             Add tmt dependencies to the supplied container image or image built
             from the supplied Containerfile.
             This will cause a derived image to be built from the supplied image.
             """)

    install_opts: Optional[str] = field(
        default=None,
        option=('--install-opts'),
        metavar='INSTALLOPTS',
        help="""
             Additional options to pass to the invocation of bootc install.
             These are passed directly to bootc install to-disk so pay close attention
             to the format of the string. Here's an example:

             --karg=nosmt --karg=console=ttyS0,114800n8
             """)


@tmt.steps.provides_method('bootc')
class ProvisionBootc(tmt.steps.provision.ProvisionPlugin[BootcData]):
    """
    Provision a local virtual machine using a bootc container image

    Minimal config which uses the Fedora bootc image:

    .. code-block:: yaml

        provision:
            how: bootc
            containerimage: quay.io/fedora/fedora-bootc:40

    Here's a config example using a containerfile:

    .. code-block:: yaml

        provision:
            how: bootc
            containerfile: "./my-custom-image.containerfile"
            containerfile-workdir: .
            image_builder: quay.io/centos-bootc/bootc-image-builder:stream9
            disk: 100

    Another config example using an image that includes tmt dependencies:

    .. code-block:: yaml

        provision:
            how: bootc
            add_deps: false
            containerimage: localhost/my-image-with-deps

    This plugin is an extension of the virtual.testcloud plugin.
    Essentially, it takes a container image as input, builds a
    bootc disk image from the container image, then uses the virtual.testcloud
    plugin to create a virtual machine using the bootc disk image.

    The bootc disk creation requires running podman as root. The plugin will
    automatically check if the current podman connection is rootless. If it is,
    a podman machine will be spun up and used to build the bootc disk.
    """

    _data_class = BootcData
    _guest_class = GuestTestcloud
    _guest = None
    _rootless = True

    def _get_id(self) -> str:
        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(tmt.steps.provision.Provision, self.parent)
        assert parent.plan is not None
        assert parent.plan.my_run is not None
        assert parent.plan.my_run.unique_id is not None
        return parent.plan.my_run.unique_id

    def _expand_path(self, relative_path: str) -> str:
        """ Expand the path to the full path relative to the current working dir """
        if relative_path.startswith("/"):
            return relative_path
        return f"{os.getcwd()}/{relative_path}"

    def _build_derived_image(self, base_image: str) -> str:
        """ Build a "derived" container image from the base image with tmt dependencies added """
        assert self.workdir is not None  # narrow type

        self._logger.debug("Building modified container image with necessary tmt packages/config")
        containerfile_template = '''
            FROM {{ base_image }}

            RUN \
            dnf -y install cloud-init rsync && \
            ln -s ../cloud-init.target /usr/lib/systemd/system/default.target.wants && \
            rm /usr/local -rf && ln -sr /var/usrlocal /usr/local && mkdir -p /var/usrlocal/bin && \
            dnf clean all
        '''
        containerfile_parsed = render_template(
            containerfile_template,
            base_image=base_image)
        (self.workdir / 'Containerfile').write_text(containerfile_parsed)

        image_tag = f'localhost/tmtmodified-{self._get_id()}'
        tmt.utils.Command(
            "podman",
            "build",
            f'{self.workdir}',
            "-f",
            f'{self.workdir}/Containerfile',
            "-t",
            image_tag).run(
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger,
            env=PODMAN_ENV if self._rootless else None)

        return image_tag

    def _build_base_image(self, containerfile: str, workdir: str) -> str:
        """ Build the "base" or user supplied container image """
        image_tag = f'localhost/tmtbase-{self._get_id()}'
        self._logger.debug("Building container image")
        tmt.utils.Command(
            "podman",
            "build",
            self._expand_path(workdir),
            "-f",
            self._expand_path(containerfile),
            "-t",
            image_tag).run(
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger,
            env=PODMAN_ENV if self._rootless else None)
        return image_tag

    def _build_bootc_disk(self, containerimage: str) -> None:
        """ Build the bootc disk from a container image using bootc install """
        self._logger.debug("Building bootc disk image")

        tmt.utils.Command(
            "truncate",
            "-s",
            "10G",
            "disk.raw"
            ).run(
            log=self._logger.verbose,
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger)

        install_args = [
            "podman",
            "run",
            "--pid=host",
            "--network=host",
            "--privileged",
            "--security-opt",
            "label=type:unconfined_t",
            "-v",
            f'{CONTAINER_STORAGE_DIR}:{CONTAINER_STORAGE_DIR}',
            "-v",
            f"{self.workdir}:/output",
            "-v",
            "/dev:/dev",
            containerimage,
            "bootc",
            "install",
            "to-disk",
            "--via-loopback",
            "--generic-image",
            "--skip-fetch-check",
            ]
        if self.data.install_opts:
            for opt in self.data.install_opts.split(" "):
                install_args.append(opt)

        install_args.append("/output/disk.raw")

        tmt.utils.Command(*install_args).run(
            log=self._logger.verbose,
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger,
            env=PODMAN_ENV if self._rootless else None)

        tmt.utils.Command(
            "qemu-img",
            "convert",
            "-O",
            "qcow2",
            "disk.raw",
            "disk.qcow2"
            ).run(
            log=self._logger.verbose,
            cwd=self.workdir,
            stream_output=True,
            logger=self._logger)

    def _init_podman_machine(self) -> None:
        try:
            tmt.utils.Command(
                "podman", "machine", "rm", "-f", PODMAN_MACHINE_NAME
                ).run(cwd=self.workdir, stream_output=True, logger=self._logger)
        except BaseException:
            self._logger.debug("Unable to remove existing podman machine (it might not exist)")

        self._logger.debug("Initializing podman machine")
        tmt.utils.Command(
            "podman", "machine", "init", "--rootful",
            "--disk-size", f"{DEFAULT_PODMAN_MACHINE_DISK_SIZE.magnitude}",
            "--memory", f"{DEFAULT_PODMAN_MACHINE_MEM.magnitude}",
            "--cpus", f"{DEFAULT_PODMAN_MACHINE_CPU}",
            "-v", f"{DEFAULT_TMP_PATH}:{DEFAULT_TMP_PATH}",
            "-v", "$HOME:$HOME",
            PODMAN_MACHINE_NAME
            ).run(cwd=self.workdir, stream_output=True, logger=self._logger)

        self._logger.debug("Starting podman machine")
        tmt.utils.Command(
            "podman", "machine", "start", PODMAN_MACHINE_NAME
            ).run(cwd=self.workdir, stream_output=True, logger=self._logger)

    def _check_if_podman_is_rootless(self) -> None:
        output = tmt.utils.Command(
            "podman", "info", "--format", "{{.Host.Security.Rootless}}"
            ).run(cwd=self.workdir, stream_output=True, logger=self._logger)
        self._rootless = output.stdout == "true\n"

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """ Provision the bootc instance """
        super().go(logger=logger)

        self._check_if_podman_is_rootless()

        data = BootcData.from_plugin(self)
        data.image = f"file://{self.workdir}/disk.qcow2"
        data.show(verbose=self.verbosity_level, logger=self._logger)

        if self._rootless:
            self._init_podman_machine()

        if data.containerimage is not None:
            containerimage = data.containerimage
            if data.add_deps:
                containerimage = self._build_derived_image(data.containerimage)
            self._build_bootc_disk(containerimage)
        elif data.containerfile is not None:
            containerimage = self._build_base_image(data.containerfile, data.containerfile_workdir)
            if data.add_deps:
                containerimage = self._build_derived_image(containerimage)
            self._build_bootc_disk(containerimage)
        else:
            raise tmt.utils.ProvisionError(
                "Either containerfile or containerimage must be specified.")

        self._guest = GuestBootc(
            logger=self._logger,
            data=data,
            name=self.name,
            parent=self.step,
            containerimage=containerimage,
            rootless=self._rootless)
        self._guest.start()
        self._guest.setup()

    def guest(self) -> Optional[tmt.steps.provision.Guest]:
        return self._guest
