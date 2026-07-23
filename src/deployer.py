import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional
from base64 import b64decode, b64encode

from jinja2 import Template
from kubernetes import client
from kubernetes.client import ApiException
from kubernetes.config import load_incluster_config
from kubernetes.client import V1Secret

from lifecycle.auth.job_family import get_job_family_jwt_token
from lifecycle.config import Config
from lifecycle.deployer.base import JobDeployer
from lifecycle.deployer.secrets import JobSecrets
from racetrack_client.client.env import merge_env_vars
from racetrack_client.client_config.client_config import Credentials
from racetrack_client.log.logs import get_logger
from racetrack_client.manifest import Manifest
from racetrack_client.manifest.manifest import ResourcesManifest
from racetrack_client.utils.datamodel import convert_to_json, parse_dict_datamodel
from racetrack_client.utils.shell import shell
from racetrack_client.utils.time import datetime_to_timestamp, now
from racetrack_commons.plugin.core import PluginCore
from racetrack_commons.plugin.engine import PluginEngine
from racetrack_commons.api.debug import debug_mode_enabled
from racetrack_commons.api.tracing import get_tracing_header_name
from racetrack_commons.deploy.image import get_job_image
from racetrack_commons.deploy.resource import job_resource_name
from racetrack_commons.entities.dto import JobDto, JobStatus, JobFamilyDto

from utils import K8S_NAMESPACE

logger = get_logger(__name__)


class KubernetesJobDeployer(JobDeployer):

    def __init__(self, src_dir: Path) -> None:
        self.infrastructure_name = 'kubernetes'
        self.src_dir = src_dir

    def deploy_job(
        self,
        manifest: Manifest,
        config: Config,
        plugin_engine: PluginEngine,
        tag: str,
        runtime_env_vars: Dict[str, str],
        family: JobFamilyDto,
        containers_num: int = 1,
        runtime_secret_vars: Dict[str, str] | None = None,
    ) -> JobDto:
        """Deploy Job on Kubernetes and expose Service accessible by Job name"""
        resource_name = job_resource_name(manifest.name, manifest.version)
        deployment_timestamp = datetime_to_timestamp(now())
        auth_token = get_job_family_jwt_token(family.name)
        manifest_yaml = manifest.origin_yaml_.replace('\n', '\\n').replace('"', '\"')

        common_env_vars = {
            'PUB_URL': config.internal_pub_url,
            'JOB_NAME': manifest.name,
            'JOB_VERSION': manifest.version,
            'JOB_MANIFEST_YAML': manifest_yaml,
            'AUTH_TOKEN': auth_token,
            'JOB_DEPLOYMENT_TIMESTAMP': deployment_timestamp,
            'REQUEST_TRACING_HEADER': get_tracing_header_name(),
            'JOB_USER_MODULE_HOSTNAME': 'localhost',
        }
        if config.open_telemetry_enabled:
            common_env_vars['OPENTELEMETRY_ENDPOINT'] = config.open_telemetry_endpoint

        plugin_vars_list = plugin_engine.invoke_plugin_hook(PluginCore.job_runtime_env_vars)
        for plugin_vars in plugin_vars_list:
            if plugin_vars:
                common_env_vars = merge_env_vars(common_env_vars, plugin_vars)

        conflicts = common_env_vars.keys() & runtime_env_vars.keys()
        if conflicts:
            raise RuntimeError(f'found illegal runtime env vars, which conflict with reserved names: {conflicts}')
        runtime_env_vars = merge_env_vars(runtime_env_vars, common_env_vars)

        resources = manifest.resources or ResourcesManifest()
        memory_min = resources.memory_min or config.default_job_memory_min
        memory_max = resources.memory_max or config.default_job_memory_max
        cpu_min = resources.cpu_min or config.default_job_cpu_min
        cpu_max = resources.cpu_max or config.default_job_cpu_max
        if resources.memory_max is None and memory_max < memory_min:
            memory_max = memory_min
        if resources.cpu_max is None and cpu_max < cpu_min:
            cpu_max = cpu_min
        if memory_min.plain_number * 4 < memory_max.plain_number:
            memory_min = memory_max / 4
            logger.info(f'minimum memory increased to memory_max/4: {memory_min}')

        assert memory_max <= config.max_job_memory_limit, \
            f'given memory limit {memory_max} is greater than max allowed {config.max_job_memory_limit}'
        assert memory_min, 'memory_min must be greater than zero'
        assert cpu_min, 'cpu_min must be greater than zero'
        assert memory_min <= memory_max, 'memory_min must be less than memory_max'
        assert cpu_min <= cpu_max, 'cpu_min must be less than cpu_max'

        render_vars = {
            'resource_name': resource_name,
            'manifest': manifest,
            'deployment_timestamp': deployment_timestamp,
            'env_vars': runtime_env_vars,
            'memory_min': memory_min,
            'memory_max': memory_max,
            'cpu_min': cpu_min,
            'cpu_max': cpu_max,
            'job_k8s_namespace': K8S_NAMESPACE,
            'runtime_secret_vars': runtime_secret_vars or {},
            'infrastructure_target': 'kubernetes',
        }
        
        container_vars = []  # list of container tuples: (container_name, image_name, container_port)
        for container_index in range(containers_num):
            container_name = get_container_name(resource_name, container_index)
            image_name = get_job_image(config.docker_registry, config.docker_registry_namespace, manifest.name, tag, container_index)
            container_port = 7000 + container_index
            container_vars.append((container_name, image_name, container_port))
        render_vars['containers'] = container_vars

        _apply_templated_resource('job_template.yaml', render_vars, self.src_dir)

        internal_name = f'{resource_name}.{K8S_NAMESPACE}.svc.cluster.local:7000'
        return JobDto(
            name=manifest.name,
            version=manifest.version,
            status=JobStatus.RUNNING.value,
            create_time=deployment_timestamp,
            update_time=deployment_timestamp,
            manifest=manifest,
            internal_name=internal_name,
            image_tag=tag,
            infrastructure_target=self.infrastructure_name,
        )

    def delete_job(self, job_name: str, job_version: str):
        k8s_client = self._k8s_api_client()
        resource_name = job_resource_name(job_name, job_version)

        apps_api = client.AppsV1Api(k8s_client)
        apps_api.delete_namespaced_deployment(resource_name, namespace=K8S_NAMESPACE)
        logger.info(f'deleted k8s deployment: {resource_name}')

        core_api = client.CoreV1Api(k8s_client)
        core_api.delete_namespaced_service(resource_name, namespace=K8S_NAMESPACE)
        logger.info(f'deleted k8s service: {resource_name}')

        try:
            core_api.delete_namespaced_secret(resource_name, namespace=K8S_NAMESPACE)
            logger.info(f'deleted k8s secret: {resource_name}')
        except ApiException as e:
            if e.reason == 'Not Found':
                logger.warning(f'k8s secret "{resource_name}" was not found')
            else:
                raise e

        try:
            custom_objects_api = client.CustomObjectsApi(k8s_client)
            custom_objects_api.delete_namespaced_custom_object('monitoring.coreos.com', 'v1', K8S_NAMESPACE, 'servicemonitors',
                                                               resource_name)
            logger.info(f'deleted k8s servicemonitor: {resource_name}')
        except ApiException as e:
            if e.reason == 'Not Found':
                logger.warning(f'k8s servicemonitor "{resource_name}" was not found')
            else:
                raise e

    def job_exists(self, job_name: str, job_version: str) -> bool:
        k8s_client = self._k8s_api_client()
        apps_api = client.AppsV1Api(k8s_client)
        try:
            resource_name = job_resource_name(job_name, job_version)
            apps_api.read_namespaced_deployment(resource_name, namespace=K8S_NAMESPACE)
            return True
        except ApiException as e:
            if e.reason == 'Not Found':
                return False
            raise e

    @staticmethod
    def _k8s_api_client() -> client.ApiClient:
        load_incluster_config()
        return client.ApiClient()

    def save_job_secrets(
        self,
        job_name: str,
        job_version: str,
        job_secrets: JobSecrets,
    ):
        """Create or update secrets needed to build and deploy a job"""
        resource_name = job_resource_name(job_name, job_version)
        encoded_runtime_vars = {}
        for var_name, var_value in job_secrets.secret_runtime_env.items():
            encoded_runtime_vars[var_name] = _encode_secret_string(var_value)
        render_vars = {
            'resource_name': resource_name,
            'job_name': job_name,
            'job_version': job_version,
            'git_credentials': _encode_secret_key(job_secrets.git_credentials),
            'secret_build_env': _encode_secret_key(job_secrets.secret_build_env),
            'secret_runtime_env': _encode_secret_key(job_secrets.secret_runtime_env),
            'job_k8s_namespace': K8S_NAMESPACE,
            'encoded_runtime_vars': encoded_runtime_vars,
        }
        _apply_templated_resource('secret_template.yaml', render_vars, self.src_dir)

    def get_job_secrets(
        self,
        job_name: str,
        job_version: str,
    ) -> JobSecrets:
        """Retrieve secrets for building and deploying a job"""
        k8s_client = self._k8s_api_client()
        core_api = client.CoreV1Api(k8s_client)

        resource_name = job_resource_name(job_name, job_version)
        try:
            secret: V1Secret = core_api.read_namespaced_secret(resource_name, namespace=K8S_NAMESPACE)
        except ApiException as e:
            if e.reason == 'Not Found':
                raise RuntimeError(f"Can't find secrets associated with job {job_name} v{job_version}")
            else:
                raise e
        secret_data: Dict[str, str] = secret.data

        secret_build_env = _decode_secret_key(secret_data, 'secret_build_env') or {}
        secret_runtime_env = _decode_secret_key(secret_data, 'secret_runtime_env') or {}
        git_credentials_dict = _decode_secret_key(secret_data, 'git_credentials')
        git_credentials = parse_dict_datamodel(git_credentials_dict, Credentials) if git_credentials_dict else None

        return JobSecrets(
            git_credentials=git_credentials,
            secret_build_env=secret_build_env,
            secret_runtime_env=secret_runtime_env,
        )


def _apply_templated_resource(template_filename: str, render_vars: Dict[str, Any], src_dir: Path):
    """Create resource from YAML template and apply it to kubernetes using kubectl apply"""
    fd, path = tempfile.mkstemp(prefix=template_filename, suffix='.yaml')
    try:
        resource_yaml = _template_resource(template_filename, render_vars, src_dir)
        with open(fd, 'w') as f:
            f.write(resource_yaml)
        shell(f'kubectl apply -f {path}')
    finally:
        if not debug_mode_enabled():
            os.remove(path)


def _template_resource(template_filename: str, render_vars: Dict[str, Any], src_dir: Path) -> str:
    """Load template from YAML, render templated vars and return as a string"""
    template_path = src_dir / 'templates' / template_filename
    override_template_path = Path('/mnt/templates') / template_filename
    if override_template_path.is_file():
        template_path = override_template_path

    template_content = template_path.read_text()
    template = Template(template_content)
    templated = template.render(**render_vars)
    return templated


def _encode_secret_key(obj: Any) -> str:
    if obj is None:
        return ''
    obj_json: str = convert_to_json(obj)
    obj_encoded: str = b64encode(obj_json.encode()).decode()
    return obj_encoded


def _decode_secret_key(secret_data: Dict[str, str], key: str) -> Optional[Any]:
    encoded = secret_data.get(key)
    if not encoded:
        return None
    decoded_json: str = b64decode(encoded.encode()).decode()
    decoded_obj = json.loads(decoded_json)
    return decoded_obj


def _encode_secret_string(text: str) -> str:
    return b64encode(text.encode()).decode()


def get_container_name(resource_name: str, container_index: int) -> str:
    if container_index == 0:
        return resource_name
    else:
        return f'{resource_name}-{container_index}'
