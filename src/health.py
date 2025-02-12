from typing import Callable

import backoff
from kubernetes import client
from kubernetes.client import V1ContainerState, V1PodStatus, V1ContainerStatus, V1ContainerStateTerminated, \
    V1ContainerStateWaiting, V1Pod, V1PodCondition

from racetrack_client.log.context_error import ContextError
from racetrack_client.utils.request import Requests, RequestError, Response
from racetrack_commons.deploy.resource import job_resource_name
from racetrack_commons.entities.dto import JobDto

from utils import k8s_api_client, get_job_pods

KNOWN_FAULTY_STATE_MESSAGES = {'Insufficient cpu', 'Insufficient memory', 'Insufficient ephemeral-storage'}
KNOWN_FAULTY_STATE_REASONS = {'FailedScheduling', 'SchedulerError', 'CrashLoopBackOff', 'FailedCreate', 'OOMKilled'}
# Maximum number of seconds to wait until the job is alive in the cluster (/live endpoint responds)
TIMEOUT_UNTIL_JOB_ALIVE: int = 15 * 60
# Maximum number of seconds to wait until the job is ready (initialized)
TIMEOUT_UNTIL_JOB_READY: int = 10 * 60


def check_until_job_is_operational(
    job: JobDto,
    deployment_timestamp: int = 0,
    on_job_alive: Callable = None,
    headers: dict[str, str] | None = None,
):
    """
    Mostly copied from racetrack/lifecycle/lifecycle/monitor/health.py
    Check liveness and readiness of a Job in a multi-stage manner:
    - wait for Job to accept HTTP connections, wait for k8s/docker to bring up the pod/container
    - check liveness endpoint if there was a critical error
    - check readiness periodically until Job is initialized
    :param job: Job data model to check
    :param deployment_timestamp: timestamp of deployment to verify if the running version is really the expected one
    If set to zero, checking version is skipped.
    :param on_job_alive: handler called when Job is live, but not ready yet
    (server running already, but still initializing)
    :param headers: headers to include when making a request
    :raise RuntimeError in case of failure
    """
    resource_name = job_resource_name(job.name, job.version)
    base_url: str = f'http://{job.internal_name}'
    k8s_client = k8s_api_client()
    core_api = client.CoreV1Api(k8s_client)

    # this can have long timeout since any potential malfunction in here is not related to a model/entrypoint
    # but the cluster errors that shouldn't happen usually
    @backoff.on_exception(backoff.fibo, RuntimeError, max_value=3,
                          max_time=TIMEOUT_UNTIL_JOB_ALIVE, jitter=None, logger=None)
    def _wait_until_job_is_alive(_base_url: str, expected_deployment_timestamp: int, _headers: dict[str, str] | None) -> Response:
        return check_job_is_alive(_base_url, core_api, resource_name, expected_deployment_timestamp, _headers)

    response = _wait_until_job_is_alive(base_url, deployment_timestamp, headers)
    _validate_live_response(response)
    if on_job_alive is not None:
        on_job_alive()

    @backoff.on_exception(backoff.fibo, TimeoutError, max_value=3,
                          max_time=TIMEOUT_UNTIL_JOB_READY, jitter=None, logger=None)
    def _wait_until_job_is_ready(_base_url: str, _headers: dict[str, str] | None = None):
        check_job_is_ready(_base_url, _headers)

    _wait_until_job_is_ready(base_url, headers)


def check_job_is_alive(
    base_url: str,
    core_api: client.CoreV1Api,
    resource_name: str,
    expected_deployment_timestamp: int,
    headers: dict[str, str] | None,
) -> Response:
    """Wait until Job resource (pod or container) is up. This catches internal cluster errors"""
    try:
        response = Requests.get(f'{base_url}/live', headers=headers, timeout=3)
    except RequestError as e:
        check_pods_are_alive(core_api, resource_name)
        raise RuntimeError(f"Cluster error: can't reach Job: {e}")

    # prevent from getting responses from the old, dying pod. Ensure new Job responds to probes
    if expected_deployment_timestamp:
        content_type = response.headers.get('content-type', '')
        assert content_type, 'Missing Content-Type header in live endpoint'
        assert 'application/json' in content_type, 'live endpoint should respond with application/json content-type'
        result: dict = response.json()
        assert isinstance(result, dict), 'live endpoint should respond with JSON object'
        assert 'deployment_timestamp' in result, 'live endpoint JSON should have "deployment_timestamp" field'
        current_deployment_timestamp = int(result.get('deployment_timestamp') or 0)
        if current_deployment_timestamp != expected_deployment_timestamp:
            check_pods_are_alive(core_api, resource_name)
            raise RuntimeError("Cluster error: can't reach newer Job, incorrect deployment_timestamp field")

    check_pods_are_alive(core_api, resource_name)
    return response


def check_pods_are_alive(core_api: client.CoreV1Api, resource_name: str) -> None:
    pods: list[V1Pod] = get_job_pods(core_api).get(resource_name) or []
    for pod in pods:
        if not pod.status:
            continue
        pod_status: V1PodStatus = pod.status
        container_statuses: list[V1ContainerStatus] = pod_status.container_statuses or []
        for container_status in container_statuses:
            state: V1ContainerState | None = container_status.state
            if state:
                state_terminated: V1ContainerStateTerminated = state.terminated
                if state_terminated:
                    if state_terminated.reason in KNOWN_FAULTY_STATE_REASONS or state_terminated.message in KNOWN_FAULTY_STATE_MESSAGES:
                        raise ValueError(f"Pod {pod.metadata.name} is in Terminated state: {state_terminated.reason}, {state_terminated.message}")
                state_waiting: V1ContainerStateWaiting = state.waiting
                if state_waiting:
                    if state_waiting.reason in KNOWN_FAULTY_STATE_REASONS or state_waiting.message in KNOWN_FAULTY_STATE_MESSAGES:
                        raise ValueError(f"Pod {pod.metadata.name} is in Waiting state: {state_terminated.reason}, {state_terminated.message}")

        # Check 'conditions' for pods, as pods might not have container_statuses initially
        container_conditions: list[V1PodCondition] = pod_status.conditions or []
        for condition in container_conditions:
            if condition:
                if condition.reason in KNOWN_FAULTY_STATE_REASONS or condition.message in KNOWN_FAULTY_STATE_MESSAGES:
                    raise ValueError(f"Failed container condition {condition.status} for Pod {pod.metadata.name}: {condition.reason}, {condition.message}")


def check_job_is_ready(base_url: str, headers: dict[str, str] | None) -> None:
    try:
        response = Requests.get(f'{base_url}/ready', headers=headers, timeout=3)
    except BaseException as e:
        raise ContextError('Job server crashed while initialization') from e
    if response.status_code == 200:
        return
    if response.status_code == 404:
        raise RuntimeError('Job health error: readiness endpoint not found')

    response = Requests.get(f'{base_url}/live', headers=headers, timeout=3)
    _validate_live_response(response)

    raise TimeoutError('Job initialization timed out')


def _validate_live_response(response: Response):
    """Check liveness probe and report error immediately once Job has crashed during startup"""
    if response.status_code == 200:
        return
    if response.status_code == 404:
        raise RuntimeError('Job health error: liveness endpoint not found')

    if 'application/json' in response.headers.get('content-type', ''):
        result = response.json()
        if isinstance(result, dict) and 'error' in result:
            raise RuntimeError(f'Job initialization error: {result.get("error")}')

    raise RuntimeError(f'Job liveness error: {response.status_code} {response.status_reason}')


def quick_check_job_condition(base_url: str, headers: dict[str, str] | None = None):
    """
    Quick check (1 attempt) if Job is live and ready.
    :param base_url: url of Job home page
    :param headers: headers to include when making a request
    :raise RuntimeError in case of failure
    """
    try:
        response = Requests.get(f'{base_url}/live', headers=headers, timeout=3)
    except RequestError as e:
        raise RuntimeError(f"Cluster error: can't reach Job: {e}")
    _validate_live_response(response)

    try:
        response = Requests.get(f'{base_url}/ready', headers=headers, timeout=3)
    except RequestError as e:
        raise RuntimeError(f"Cluster error: can't reach Job: {e}")
    if response.status_code == 200:
        return
    if response.status_code == 404:
        raise RuntimeError('Job health error: readiness endpoint not found')
    raise RuntimeError('Job is still initializing')
