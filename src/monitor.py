import json
import time
from typing import Callable, Iterable

from kubernetes import client
from kubernetes.client import V1ObjectMeta, V1PodStatus, ApiException, V1ContainerStatus

from lifecycle.config import Config
from lifecycle.monitor.base import JobMonitor
from lifecycle.monitor.metric_parser import read_last_call_timestamp_metric, scrape_metrics
from racetrack_client.log.context_error import wrap_context
from racetrack_client.log.exception import short_exception_details
from racetrack_client.utils.shell import CommandError, shell_output
from racetrack_client.utils.time import datetime_to_timestamp
from racetrack_commons.deploy.resource import job_resource_name
from racetrack_commons.entities.dto import JobDto, JobStatus
from racetrack_client.log.logs import get_logger

from utils import get_recent_job_pod, k8s_api_client, K8S_JOB_NAME_LABEL, K8S_JOB_VERSION_LABEL, \
    K8S_NAMESPACE, K8S_JOB_RESOURCE_LABEL, get_job_deployments, get_job_pods
from health import check_until_job_is_operational, quick_check_job_condition, KNOWN_FAULTY_STATE_MESSAGES, KNOWN_FAULTY_STATE_REASONS

logger = get_logger(__name__)


class KubernetesMonitor(JobMonitor):
    """Discovers Job resources in a k8s cluster and monitors their condition"""

    def __init__(self) -> None:
        self.infrastructure_name = 'kubernetes'

    def list_jobs(self, config: Config) -> Iterable[JobDto]:
        # Ideally these should be in __init__, but that breaks test_bootstrap.py
        k8s_client = k8s_api_client()
        core_api = client.CoreV1Api(k8s_client)
        apps_api = client.AppsV1Api(k8s_client)

        with wrap_context('listing Kubernetes API'):
            deployments = get_job_deployments(apps_api)
            pods_by_job = get_job_pods(core_api)

        for resource_name, deployment in deployments.items():
            pods = pods_by_job.get(resource_name)
            if pods is None or len(pods) == 0:
                continue

            recent_pod = get_recent_job_pod(pods)
            metadata: V1ObjectMeta = recent_pod.metadata
            job_name = metadata.labels.get(K8S_JOB_NAME_LABEL)
            job_version = metadata.labels.get(K8S_JOB_VERSION_LABEL)
            if not (job_name and job_version):
                continue

            start_timestamp = datetime_to_timestamp(recent_pod.metadata.creation_timestamp)
            internal_name = f'{resource_name}.{K8S_NAMESPACE}.svc.cluster.local:7000'

            replica_internal_names: list[str] = []
            restart_count = 0
            for pod in pods:
                pod_status: V1PodStatus = pod.status
                if pod_status.pod_ip:
                    pod_ip_dns: str = pod_status.pod_ip.replace('.', '-')
                    replica_internal_names.append(
                        f'{pod_ip_dns}.{resource_name}.{K8S_NAMESPACE}.svc.cluster.local:7000'
                    )
                container_statuses: list[V1ContainerStatus] = pod_status.container_statuses
                if container_statuses:
                    for container_status in container_statuses:
                        restart_count += container_status.restart_count
            replica_internal_names.sort()
            infrastructure_stats = {
                'number_of_restarts': restart_count,
            }

            job = JobDto(
                name=job_name,
                version=job_version,
                status=JobStatus.RUNNING.value,
                create_time=start_timestamp,
                update_time=start_timestamp,
                manifest=None,
                internal_name=internal_name,
                error=None,
                infrastructure_target=self.infrastructure_name,
                replica_internal_names=replica_internal_names,
                infrastructure_stats=infrastructure_stats,
            )
            try:
                job_url = self._get_internal_job_url(job)
                quick_check_job_condition(job_url)
                job_metrics = scrape_metrics(f'{job_url}/metrics')
                job.last_call_time = read_last_call_timestamp_metric(job_metrics)
            except Exception as e:
                error_details = short_exception_details(e)
                job.error = error_details
                job.status = JobStatus.ERROR.value
                logger.warning(f'Job {job} is in bad condition: {error_details}')
            yield job

    def check_job_condition(
        self, job: JobDto, deployment_timestamp: int = 0, on_job_alive: Callable = None, logs_on_error: bool = True,
    ):
        try:
            check_deployment_for_early_errors(job_resource_name(job.name, job.version))
            check_until_job_is_operational(job, deployment_timestamp, on_job_alive)
        except Exception as e:
            if logs_on_error:
                try:
                    logs = self.read_recent_logs(job)
                except (AssertionError, ApiException, CommandError):
                    raise RuntimeError(str(e)) from e
                raise RuntimeError(f'{e}\nJob logs:\n{logs}') from e
            else:
                raise RuntimeError(str(e)) from e

    def read_recent_logs(self, job: JobDto, tail: int = 20) -> str:
        resource_name = job_resource_name(job.name, job.version)
        return shell_output(f'kubectl logs'
                            f' --selector {K8S_JOB_RESOURCE_LABEL}={resource_name}'
                            f' -n {K8S_NAMESPACE}'
                            f' --tail={tail}'
                            f' --container={resource_name}')

    def _get_internal_job_url(self, job: JobDto) -> str:
        return f'http://{job.internal_name}'


def check_deployment_for_early_errors(resource_name: str):
    """
    Check for indicators of early errors in the deployment.
    If errors were found, raises an error. If no errors were found, returns.
    If unsure, continues checking until timeout, then returns.
    """
    attempts = 0
    loop_sleep_time = 5
    start_time = time.time()
    timeout = start_time + 10 * 60  # (n_minutes * seconds/minute) - we experienced a deployment failing after 8 minutes.
    while time.time() < timeout:
        is_event_okay, events_info = check_job_events(resource_name)
        attempts += 1
        if is_event_okay is True:
            logger.info(f'Deployment {resource_name} looks good in Kubernetes after {time.time() - start_time:.2f} seconds, {attempts} attempts')
            return
        if is_event_okay is False:
            raise RuntimeError(f'Found faulty events in Kubernetes for {resource_name}: {events_info}')
        time.sleep(loop_sleep_time)


def check_job_events(resource_name: str) -> tuple[bool | None, str]:
    """A "check" returns True on deployment success, False on deployment failure, and None if unsure."""
    deployment_events = get_events('Deployment', resource_name)

    cmd = f'kubectl get replicaset --sort-by=\'.metadata.creationTimestamp\' -o json --namespace {K8S_NAMESPACE} --selector {K8S_JOB_RESOURCE_LABEL}={resource_name}'
    replicasets = json.loads(shell_output(cmd))['items']
    if not replicasets:
        return False, f'No Replica Set found for a deployment {resource_name}'
    replicaset_name = replicasets[-1]['metadata']['name']
    replicaset_template_hash = replicasets[-1]['metadata']['labels'].get('pod-template-hash')
    replicaset_events = get_events('ReplicaSet', replicaset_name)

    cmd = f'kubectl get pods -o json --namespace {K8S_NAMESPACE} --selector pod-template-hash={replicaset_template_hash}'
    pods = json.loads(shell_output(cmd))['items']
    pod_names = [pod.get('metadata', {}).get('name') for pod in pods]
    pod_events = sum((get_events('Pod', pod_name) for pod_name in pod_names), [])

    return validate_events(pod_events + replicaset_events + deployment_events)


def get_events(kind: str, resource_name: str) -> list[dict]:
    cmd = f'kubectl get events' \
        f' --namespace {K8S_NAMESPACE}' \
        f' --sort-by=\'.metadata.creationTimestamp\'' \
        f' -o json' \
        f' --field-selector involvedObject.kind={kind},involvedObject.name={resource_name}'
    events_query = json.loads(shell_output(cmd))
    return events_query.get('items', [])[::-1]  # list of CoreV1Event dictionaries


def validate_events(events: list[dict]) -> tuple[bool | None, str]:
    for event in events:  # CoreV1Event
        resource_name = event.get('metadata', {}).get('name')
        reason = event.get('reason')
        message = event.get('message')
        if reason == 'Started':
            return True, ''
        if event.get('type') == 'Warning':
            for error in KNOWN_FAULTY_STATE_MESSAGES:
                if error in message:
                    return False, f'{resource_name} has produced erroneous event message: {message}'
            if reason in KNOWN_FAULTY_STATE_REASONS:
                return False, f'{resource_name} has produced erroneous event reason: {reason}'
    return None, ''
