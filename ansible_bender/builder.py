"""
Builder interface
"""
import logging

from ansible_bender.builders.buildah_builder import BuildahBuilder
from ansible_bender.builders.docker_builder import DockerBuilder


logger = logging.getLogger(__name__)


BUILDERS = {
    BuildahBuilder.name: BuildahBuilder,
    DockerBuilder.name: DockerBuilder
}


def get_builder(builder_name):
    try:
        return BUILDERS[builder_name]
    except KeyError:
        raise RuntimeError("No such builder %s" % builder_name)
