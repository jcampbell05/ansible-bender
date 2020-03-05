import datetime
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List

from ansible_bender.builders.base import Builder
from ansible_bender.constants import TIMESTAMP_FORMAT_TOGETHER
from ansible_bender.utils import graceful_get, run_cmd, docker_command_exists


logger = logging.getLogger(__name__)


def inspect_resource(resource_type, resource_id):
    try:
        i = run_cmd(["docker", "inspect", "--type", resource_type, resource_id],
                    return_output=True, log_output=False)
    except subprocess.CalledProcessError:
        logger.info("no such %s %s", resource_type, resource_id)
        return None
    try:
        metadata = json.loads(i)
    except IndexError:
        logger.info("no such %s %s", resource_type, resource_id)
        return None
    return metadata


def get_docker_image_id(container_image):
    metadata = inspect_resource("image", container_image)

    if len(metadata) > 0:
        return graceful_get(metadata[0], "Id")

    return None


def pull_docker_image(container_image):
    run_cmd(["docker", "pull", "--quiet", container_image],
            save_output_in_exc=False,
            log_stderr=False, print_output=True, log_output=False)


def does_image_exist(container_image):
    cmd = ["docker", "inspect", "--type", "image", container_image]
    run_cmd(cmd, print_output=False)


def docker_run_cmd(container_image, cmd, log_stderr=True, return_output=False):
    """
    run provided command in selected container image using docker; raise exc when command fails

    :param container_image: str
    :param cmd: list of str
    :param log_stderr: bool, log errors to stdout as ERROR level
    :param return_output: bool, if True, return output of the command
    :return: stdout output
    """
    return run_cmd(["docker", "run", "--rm", container_image] + cmd,
                   return_output=return_output, log_stderr=log_stderr)


def docker_run_cmd_in_container(
        container_image: str,
        host_name: str,
        cmd: List[str],
        log_stderr: bool = True,
        log_output: bool = False,
        extra_from_args: Optional[List[str]] = None,
    ):
    """
    run provided command in selected container image using docker; raise exc when command fails

    :param container_image: str
    :param host_name: str
    :param cmd: list of str
    :param log_stderr: bool, log errors to stdout as ERROR level
    :param log_output: bool, print output of the command to logs
    :param extra_from_args: a list of extra arguments for `buildah from`
    """
    container_name = "{}-{}".format(host_name, datetime.datetime.now().strftime(TIMESTAMP_FORMAT_TOGETHER))
    # was the temporary container created? if so, remove it
    created = False
    try:
        create_docker_container(
            container_image, container_name, build_volumes=None, extra_from_args=extra_from_args, command=cmd, debug=False)
        created = True
    except subprocess.CalledProcessError:
        logger.error(f"Unable to create or run a container using {container_image} with docker")
        raise
    finally:
        if created:
            run_cmd(["docker", "rm", container_name], log_stderr=log_stderr)

def create_docker_container(container_image, container_name, build_volumes=None, extra_from_args=None, command=None, debug=False):
    """
    Create new docker container according to spec.

    :param container_image: name of the image
    :param container_name: name of the container to work in
    :param extra_from_args: a list of extra arguments for `docker run`
    :param build_volumes: list of str, bind-mount specification: ["/host:/cont", ...]
    :param debug: bool, make docker print debug info
    """
    args = []
    if build_volumes:
        for volume in build_volumes:
            args += ["-v", volume]
    if not extra_from_args is None:
        args += shlex.split(extra_from_args)
    args += ["--name", container_name, container_image]

    if not command is None:
        args += command   

    # will pull the image by default if it's not present in dockers's storage
    docker("run", args, debug=debug, log_stderr=True)

def configure_docker_container(container_name, working_dir=None, env_vars=None,
                                labels=None, annotations=None,
                                user=None, cmd=None, entrypoint=None,
                                ports=None, volumes=None,
                                debug=False):
    """
    apply metadata on the container so they get inherited in an image

    :param container_name: name of the container to work in
    :param working_dir: str, path to a working directory within container image
    :param labels: dict with labels
    :param annotations: dict with annotations
    :param env_vars: dict with env vars
    :param cmd: str, command to run by default in the container
    :param entrypoint: str, entrypoint script to configure for the container
    :param user: str, username or uid; the container gets invoked with this user by default
    :param ports: list of str, ports to expose from container by default
    :param volumes: list of str; paths within the container which has data stored outside
                    of the container
    :param debug: bool, make buildah print debug info
    """
    config_args = []
    if working_dir:
        config_args += ["--workingdir", working_dir]
    if env_vars:
        for k, v in env_vars.items():
            config_args += ["-e", "%s=%s" % (k, v)]
    if labels:
        for k, v in labels.items():
            config_args += ["-l", "%s=%s" % (k, v)]
    if annotations:
        for k, v in annotations.items():
            config_args += ["--annotation", "%s=%s" % (k, v)]
    if user:
        if os.getuid() != 0:
            logger.warning(
                "CHANGING USER IN ROOTLESS MODE IS DISCOURAGED, "
                "USE IT ONLY WHEN YOU KNOW WHAT YOU'RE DOING, "
                "YOU'VE BEEN WARNED"
            )
        config_args += ["--user", user]
    if cmd:
        config_args += ["--cmd", cmd]
    if entrypoint:
        config_args += ["--entrypoint", entrypoint]
    if ports:
        for p in ports:
            config_args += ["-p", p]
    if volumes:
        for v in volumes:
            config_args += ["-v", v]
    if config_args:
        docker("config", config_args + [container_name], debug=debug)
    return container_name


def docker(command, args_and_opts, print_output=False, debug=False, log_stderr=False):
    cmd = ["docker"]
    cmd += [command] + args_and_opts
    logger.debug("running command: %s", command)
    return run_cmd(cmd, print_output=print_output, log_stderr=log_stderr)


def buildah_with_output(command, args_and_opts, debug=False):
    print("BOBX")
    cmd = ["buildah"]
    # if debug:
    #     cmd += ["--debug"]
    cmd += [command] + args_and_opts
    output = run_cmd(cmd, return_output=True)
    logger.debug("output: %s", output)
    return output


class DockerBuilder(Builder):
    ansible_connection = "docker"
    name = "docker"

    def __init__(self, build, debug=False):
        """
        :param build: instance of Build
        :param debug: bool, run buildah in debug or not?
        """
        super().__init__(build, debug=debug)
        self.target_image = build.target_image
        self.ansible_host = build.build_container
        self.logs = []
        docker_command_exists()

    def create(self):
        """
        create a container where all the work happens
        """
        create_docker_container(
            self.build.get_top_layer_id(), self.ansible_host,
            build_volumes=self.build.build_volumes,
            extra_from_args=self.build.buildah_from_extra_args,
            debug=self.debug)

    def run(self, image_name, command):
        """
        run provided command in the selected image and return output

        :param image_name: str
        :param command: list of str
        :return: str (output)
        """
        print("HUY")

        # let's apply configuration before execing the playbook, except for user
        # configure_docker_container(
        #     self.ansible_host, working_dir=self.build.metadata.working_dir,
        #     user=self.build.build_user,
        #     env_vars=self.build.metadata.env_vars,
        #     ports=self.build.metadata.ports,
        #     labels=self.build.metadata.labels,  # labels are not applied when they are configured
        #                                         # before doing commit
        #     annotations=self.build.metadata.annotations,
        #     debug=self.debug
        # )

        cmd = ["podman", "run", "--rm", image_name] + command
        return run_cmd(cmd, return_output=True)

    def swap_working_container(self):
        """
        remove current working container and replace it with the provided one
        """
        self.clean()
        self.create()

    def commit(self, image_name: Optional[str] = None, print_output: bool = True, final_image: bool = False):
        """
        commit container into an image

        :param image_name: name of the image
        :param print_output: print to stdout if True
        :param final_image: is this is the final layer?
        :return:
        """
        if final_image:
            user = self.build.metadata.user
        else:
            user = self.build.build_user

        # if (self.build.metadata.user or self.build.metadata.cmd or
        #     self.build.metadata.entrypoint or self.build.metadata.volumes):
        #     # change user if needed
        #     configure_docker_container(
        #         self.ansible_host,
        #         user=user,
        #         cmd=self.build.metadata.cmd,
        #         entrypoint=self.build.metadata.entrypoint,
        #         volumes=self.build.metadata.volumes,
        #     )

        if image_name:
            args = [self.ansible_host, image_name]
            if final_image and self.build.squash:
                args.insert(0, "--squash")
            docker("commit", args, print_output=print_output, debug=self.debug)
            return self.get_image_id(image_name)
        else:
            fd, name = tempfile.mkstemp()
            os.close(fd)
            args = ["-q", "--iidfile", name, self.ansible_host]
            # buildah 1.7.3 dropped the requirement for image name, let's support both
            # https://github.com/ansible-community/ansible-bender/issues/166
            if self.get_buildah_version() < (1, 7, 3):
                args += ["{}-{}".format(
                    self.ansible_host,
                    datetime.datetime.now().strftime(TIMESTAMP_FORMAT_TOGETHER)
                )]
            if final_image and self.build.squash:
                args.insert(0, "--squash")
            try:
                docker("commit", args, print_output=print_output, debug=self.debug)
                image_id = Path(name).read_text()
                logger.debug("layer id = %s", image_id)
                return image_id
            finally:
                os.unlink(name)

    def clean(self):
        """
        clean working container
        """
        docker("rm", [self.ansible_host], debug=self.debug)

    def get_image_id(self, image_name):
        """ return image_id for provided image """
        image_id = get_docker_image_id(image_name)
        if not image_id:
            raise RuntimeError("We haven't got any image ID: the image is not present "
                               "or docker is malfunctioning.")
        return image_id

    def is_image_present(self, image_reference):
        """
        :return: True when the selected image is present, False otherwise
        """
        if not image_reference:
            return False
        try:
            does_image_exist(image_reference)
        except subprocess.CalledProcessError:
            return False
        else:
            return True

    def pull(self):
        """
        pull base image
        """
        logger.info("pulling base image: %s", self.build.base_image)
        pull_docker_image(self.build.base_image)

    def push(self, build, target, force=False):
        """
        push built image into a remote location using `podman push`

        :param target: str, transport:details
        :param build: instance of Build
        :param force: bool, bypass checks if True
        :return: None
        """
        print("BOB LAND")
        built_image = build.get_target_image_id()
        cmd = ["buildah", "push", built_image, target]
        # podman prints progress to stderr
        run_cmd(cmd, print_output=False, log_stderr=False)

    def find_python_interpreter(self):
        """
        find python executable in the base image

        :return: str, path to python interpreter
        """
        for i in self.python_interpr_prio:
            cmd = ["ls", i]
            try:
                run_cmd(["docker", "run", "--rm", self.build.base_image] + cmd,
                        log_stderr=False, log_output=True)
            except subprocess.CalledProcessError:
                logger.info("python interpreter %s does not exist", i)
                continue
            else:
                logger.info("using python interpreter %s", i)
                return i
        logger.error("couldn't locate python interpreter, tried these paths: %s", self.python_interpr_prio)
        raise RuntimeError(f"no python interpreter was found in the base image \"{self.build.base_image}\""
                           ", you can specify the path via CLI option --python-interpreter")

    def get_logs(self):
        """
        obtain logs for the selected build

        :return: list of str
        """

    def sanity_check(self):
        """
        invoke container tooling and thus verify they work well
        """
        logger.debug("checking that docker command works")
        run_cmd(["docker", "version"], log_stderr=True, log_output=True)
        logger.debug("Checking container creation using docker")
        docker_run_cmd_in_container(
            self.build.base_image, self.ansible_host, ["true"],
            log_stderr=True, extra_from_args=self.build.buildah_from_extra_args)

    def get_buildah_version(self):
        print("BOB1")
        out = run_cmd(["buildah", "version"], log_stderr=True, return_output=True, log_output=False)
        version = re.findall(r"Version:\s*([\d\.]+)", out)[0].split(".")
        logger.debug("buildah version = %s", version)
        # buildah version = ['1', '11', '3']
        try:
            return tuple(map(int, version))
        except (IndexError, ValueError) as ex:
            logger.error("Unable to parse buildah's version: %s", ex)
            return 0, 0, 0

    def check_container_creation(self):
        """
        check that containers can be created
        """
        logger.debug("trying to create a dummy container using docker")
        docker_run_cmd(self.build.base_image, ["true"], log_stderr=True)