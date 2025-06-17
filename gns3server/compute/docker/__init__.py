# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Docker server module.
"""

import os
import sys
import json
import asyncio
import logging
import aiohttp
import shutil
import platformdirs
import docker

from gns3server.utils import parse_version
from gns3server.config import Config
from gns3server.utils.asyncio import locking
from gns3server.compute.base_manager import BaseManager
from gns3server.compute.docker.docker_vm import DockerVM
from gns3server.compute.docker.docker_error import DockerError, DockerHttp304Error, DockerHttp404Error

log = logging.getLogger(__name__)


# Be careful to keep it consistent
DOCKER_MINIMUM_API_VERSION = "1.25"
DOCKER_MINIMUM_VERSION = "1.13"
DOCKER_PREFERRED_API_VERSION = "1.30"
CHUNK_SIZE = 1024 * 8  # 8KB


class Docker(BaseManager):

    _NODE_CLASS = DockerVM

    def __init__(self):
        super().__init__()
        self.client = docker.from_env(version=DOCKER_MINIMUM_API_VERSION,DOCKER_HOST='unix://var/run/docker.sock',timeout=60)
        
        # Allow locking during ubridge operations

        self.ubridge_lock = asyncio.Lock()
        self._connector = None
        self._session = None
        self._api_version = DOCKER_MINIMUM_API_VERSION

    @staticmethod
    async def install_busybox(dst_dir):

        dst_busybox = os.path.join(dst_dir, "bin", "busybox")
        if os.path.isfile(dst_busybox):
            return
        for busybox_exec in ("busybox-static", "busybox.static", "busybox"):
            busybox_path = shutil.which(busybox_exec)
            if busybox_path:
                try:
                   # check that busybox is statically linked
                    # (dynamically linked busybox will fail to run in a container)
                    proc = await asyncio.create_subprocess_exec(
                        "ldd",
                        busybox_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode == 1:
                        # ldd returns 1 if the file is not a dynamic executable
                        log.info(f"Installing busybox from '{busybox_path}' to '{dst_busybox}'")
                        shutil.copy2(busybox_path, dst_busybox, follow_symlinks=True)
                        return
                    else:
                        log.warning(f"Busybox '{busybox_path}' is dynamically linked\n"
                                    f"{stdout.decode('utf-8', errors='ignore').strip()}")
                except OSError as e:
                    raise DockerError(f"Could not install busybox: {e}")
        raise DockerError("No busybox executable could be found, please install busybox (apt install busybox-static on Debian/Ubuntu) and make sure it is in your PATH")

    @staticmethod
    def resources_path():
        """
        Get the Docker resources storage directory
        """

        server_config = Config.instance().get_section_config("Server")
        appname = vendor = "GNS3"
        resources_path = os.path.expanduser(server_config.get("resources_path", platformdirs.user_data_dir(appname, vendor, roaming=True)))
        docker_resources_dir = os.path.join(resources_path, "docker")
        os.makedirs(docker_resources_dir, exist_ok=True)
        return docker_resources_dir

    async def install_resources(self):
       """
       Copy the necessary resources to a writable location and install busybox
       """

       try:
           dst_path = self.resources_path()
           log.info(f"Installing Docker resources in '{dst_path}'")
           from gns3server.controller import Controller
           Controller.instance().install_resource_files(dst_path, "compute/docker/resources")
           await self.install_busybox(dst_path)
       except OSError as e:
           raise DockerError(f"Could not install Docker resources to {dst_path}: {e}")

    def check_connection(self):
        try:
            self.client.ping()
            return True
        except Exception as e:
            raise DockerError(f"Can't connect to docker daemon: {e}")

    def version(self):
        try:
            version_info = self.client.version()
            api_version = parse_version(version_info['ApiVersion'])
            if api_version < parse_version(DOCKER_MINIMUM_API_VERSION):
                raise DockerError(
                    f"Docker version is {version_info['Version']}. GNS3 requires a minimum version of {DOCKER_MINIMUM_VERSION}"
                )
            return version_info
        except Exception as e:
            raise DockerError(f"Failed to get Docker version: {e}")

    def create_container(self, **kwargs):
        try:
            container = self.client.containers.create(**kwargs)
            log.debug(f"Created Docker container with ID: {container.id}")
            return container
        except docker.errors.ImageNotFound as e:
            raise DockerError(f"Image not found: {e}")
        except docker.errors.APIError as e:
            raise DockerError(f"Failed to create container: {e}")
    
    def start_container(self, container_id):
        try:
            container = self.client.containers.get(container_id)
            container.start()
            log.info(f"Started Docker container with ID: {container.id}")
        except docker.errors.NotFound as e:
            raise DockerError(f"Container not found: {e}")
        except docker.errors.APIError as e:
            raise DockerError(f"Failed to start container: {e}")

    @locking
    async def pull_image(self, image, progress_callback=None):
       """
       Pulls an image from the Docker repository
       :params image: Image name
       :params progress_callback: A function that receive a log message about image download progress
       """
       if progress_callback:
           progress_callback(f"Pulling '{image}' from Docker repository")
       try:
           for line in self.client.images.pull(image, stream=True, version=DOCKER_MINIMUM_API_VERSION):
               status = json.loads(line.decode('utf-8'))
               if 'progress' in status and progress_callback:
                   progress_callback(f"Pulling image {image}: {status.get('progress', '')}")
               elif 'status' in status and progress_callback:
                   progress_callback(f"Pulling image {image}: {status['status']}")
           if progress_callback:
               progress_callback(f"Successfully pulled image {image}")
       except docker.errors.APIError as e:
           raise DockerError(f"Could not pull image '{image}': {e}")

    async def list_images(self):
        """
        Gets Docker image list.

        :returns: list of dicts
        :rtype: list
        """
        try:
            images = [{'image': tag}
                      for image in self.client.images.list()
                      for tag in image.tags if tag != "<none>:<none>"]
            return sorted(images, key=lambda i: i['image'])
        except docker.errors.APIError as e:
            raise DockerError(f"Failed to list images: {e}")
