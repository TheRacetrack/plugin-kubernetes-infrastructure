import json
import time

from typing import Callable, Iterable, Any

from kubernetes import client
from kubernetes.client import V1ObjectMeta, V1PodStatus, ApiException, V1ContainerStatus

from lifecycle.config import Config
from lifecycle.monitor.base import JobMonitor
from lifecycle.monitor.health import check_until_job_is_operational, quick_check_job_condition
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

    def check_job_condition(self,
                            job: JobDto,
                            deployment_timestamp: int = 0,
                            on_job_alive: Callable = None,
                            logs_on_error: bool = True,
                            ):
        try:
            self.check_deployment_for_early_errors(job_resource_name(job.name, job.version))
            check_until_job_is_operational(self._get_internal_job_url(job),
                                           deployment_timestamp, on_job_alive)
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


    def check_deployment_for_early_errors(self, resource_name: str):
        """
        Check for indicators of early errors in the deployment
        If errors were found, raises an error
        If no errors were found, returns
        If unsure, continues checking until timeout, then returns
        """
        # There is not a singular way to determine if a deployment has failed early, so we combine multiple types of checks,
        # A "check" returns True on deployment success, False on deployment failure, and None if unsure.
        # The timeouts, number of checks, and loop_sleeps are mostly based on heuristics and vibes:
        #   We don't want to spam the cluster with requests
        #   But we also do not want the deployment to take much longer
        # TODO: If we are really confident in 'kubectl rollout status' we could spawn it in a separate thread and wait for it to finish with a watch='true'
        loop_sleep_time = 8
        start_time = time.time()
        timeout = start_time + 10 * 60 # (n_minutes * seconds/minute) - we experienced a deploy failing after 8 minutes.
        now = time.time()
        failed = False
        while now < timeout:
            is_rollout_okay, rollout_status = self.check_rollout_status(resource_name)
            is_event_okay, events = self.check_events(resource_name)

            if is_rollout_okay or is_event_okay:
                logger.info(f'Deployment looks good for {resource_name} after {now - start_time:.2f} seconds')
                return

            if is_rollout_okay is not None:
                if not is_rollout_okay:
                    failed = True

            if is_event_okay is not None:
                if not is_event_okay:
                    failed = True

            if failed:
                error_msg = f'Early errors detected in {resource_name}. Rollout status: {rollout_status}. Kubernetes events: {events}'
                raise RuntimeError(error_msg)

            time.sleep(loop_sleep_time)
            now = time.time()


    def check_rollout_status(self, resource_name: str, watch: str = 'false') -> tuple[bool | None, str]:
        # Some possible outputs:
        # https://github.com/kubernetes/kubernetes/blob/f1d63237edf908aae577d3da60276151c18ffee0/staging/src/k8s.io/kubectl/pkg/polymorphichelpers/rollout_status.go#L89
        successful_rollouts_messages =[
            f'deployment \"{resource_name}\" successfully rolled out',
            ]
        failure_rollouts_messages = [
            f'deployment \"{resource_name}\" exceeded its progress deadline'
        ]

        cmd = f'kubectl rollout status deployment/{resource_name} --watch={watch} --namespace {K8S_NAMESPACE}'
        rollout_status = shell_output(cmd)

        if any(msg in rollout_status for msg in successful_rollouts_messages):
            return True, rollout_status
        if any(msg in rollout_status for msg in failure_rollouts_messages):
            return False, rollout_status
        return None, rollout_status


    def check_events(self, resource_name: str) -> tuple[bool | None, str]:
        # TODO: A single "kubectl get events" command could be used to get all the events at once, but this is easier to reason about

        # First, query relevant Deployment events based on resource_name, e.g. "job-adder-v-0-0-2"
        base_cmd = f'kubectl get events ' + \
            f'--namespace {K8S_NAMESPACE} ' + \
            f'--sort-by=\'.metadata.creationTimestamp\' ' + \
            f'-o json'
        deployment_selectors = f'--field-selector involvedObject.kind=Deployment,involvedObject.name={resource_name}'
        deployment_query = json.loads(shell_output(f'{base_cmd} {deployment_selectors}'))

        # From the deployment query, we extract the relevant ReplicaSet names
        replicaset_names = self.get_replicaset_names_from_deployment_query(deployment_query, resource_name)

        # Query for all the ReplicaSets
        replicaset_queries = [
            json.loads(shell_output(f'{base_cmd} --field-selector involvedObject.kind=ReplicaSet,involvedObject.name={name}'))
            for name in replicaset_names
        ]

        errors_we_dont_want_in_messages = ['Insufficient cpu', 'Insufficient memory', 'Insufficient ephemeral-storage']
        errors_we_dont_want_in_reasons = ['FailedScheduling', 'SchedulerError', 'CrashLoopBackOff', 'FailedCreate']
        status, msg = self.check_queries_for_errors(replicaset_queries, errors_we_dont_want_in_messages, errors_we_dont_want_in_reasons)
        if status is not None:
            return status, msg

        # From the ReplicaSet queries, we get the relevant Pod names
        pod_names = self.get_pod_names_from_replicaset_queries(replicaset_queries, resource_name)

        # Query for all the Pods
        pod_queries = [
            json.loads(shell_output(f'{base_cmd} --field-selector involvedObject.kind=Pod,involvedObject.name={pod_name}'))
            for pod_name in pod_names
        ]

        status, msg = self.check_queries_for_errors(pod_queries, errors_we_dont_want_in_messages, errors_we_dont_want_in_reasons)
        if status is not None:
            return status, msg

        return None, "Events regarding deployments are inconclusive"


    def check_queries_for_errors(self, queries: list[dict[str, Any]], msg_errors: list[str], reason_errors: list[str]) -> tuple[bool | None, str]:
        for query in queries:
            for event in query.get('items', []):
                if event['reason'] == 'Started':
                    return True, event
                for error in msg_errors:
                    if error in event['message'] and event['type'] == 'Warning':
                        return False, event
                for error in reason_errors:
                    if error in event['reason'] and event['type'] == 'Warning':
                        return False, event

        return None, "Events regarding deployments are inconclusive"


    def get_replicaset_names_from_deployment_query(self, deployment_query: dict[str, Any], resource_name: str) -> set[str]:
        # Assumes events in the query are sorted from oldest to newest, so the latest one should be last element
        # We only look at the latest event from the query, hence the [-1]
        deployment_messages = [event['message'] for event in [deployment_query.get('items', [])[-1]]]
        replicaset_names = [
            self.get_first_word_with(message, f'{resource_name}-') # e.g. "job-adder-v-0-0-2-". note the trailing dash
            for message in deployment_messages
        ]

        return set(replicaset_names)


    def get_pod_names_from_replicaset_queries(self, replicaset_queries: list[dict[str, Any]], resource_name: str) -> list[str]:
        pod_names = [
            self.get_first_word_with(message, f'{resource_name}-')
            for replicaset_query in replicaset_queries
            for message in [event['message'] for event in replicaset_query.get('items', [])]
        ]
        return pod_names


    def get_first_word_with(self, string_of_words: str, starting_with: str) -> str:
        word = next((word for word in string_of_words.split() if starting_with in word), '')
        word = word.replace('"', '') # Sometimes the word is surrounded by double quotes, which is not standard
        return word
