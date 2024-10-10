from typing import Callable

import backoff

from racetrack_client.log.context_error import ContextError
from racetrack_client.utils.request import Requests, RequestError, Response
from lifecycle.server.cache import LifecycleCache
from racetrack_commons.deploy.resource import job_resource_name
from racetrack_commons.entities.dto import JobDto

from utils import k8s_api_client, get_job_pods

from kubernetes import client

ERRORS_WE_DONT_WANT_IN_MESSAGES = ['Insufficient cpu', 'Insufficient memory', 'Insufficient ephemeral-storage', 'back-off 10s restarting failed']
ERRORS_WE_DONT_WANT_IN_REASONS = ['FailedScheduling', 'SchedulerError', 'CrashLoopBackOff', 'FailedCreate', 'OOMKilled']

def check_until_job_is_operational(
    job: JobDto,
    deployment_timestamp: int = 0,
    on_job_alive: Callable = None,
    headers: dict[str, str] | None = None,
):
    """
    Mostly copied from racetrack/lifecycle/lifecycle/monitor/health.py, but we wanted to checks for kubernetes errors in the liviness check
    """
    resource_name = job_resource_name(job.name, job.version)
    base_url: str = f'http://{job.internal_name}'
    k8s_client = k8s_api_client()
    core_api = client.CoreV1Api(k8s_client)

    # this can have long timeout since any potential malfunction in here is not related to a model/entrypoint
    # but the cluster errors that shouldn't happen usually
    @backoff.on_exception(backoff.fibo, RuntimeError, max_value=3,
                          max_time=LifecycleCache.config.timeout_until_job_alive, jitter=None, logger=None)
    def _wait_until_job_is_alive(_base_url: str, core_api, resource_name, expected_deployment_timestamp: int, _headers: dict[str, str] | None) -> Response:
        return check_job_is_alive(_base_url, core_api, resource_name, expected_deployment_timestamp, _headers)

    response = _wait_until_job_is_alive(base_url, core_api, resource_name, deployment_timestamp, headers)
    _validate_live_response(response)
    if on_job_alive is not None:
        on_job_alive()

    @backoff.on_exception(backoff.fibo, TimeoutError, max_value=3,
                          max_time=LifecycleCache.config.timeout_until_job_ready, jitter=None, logger=None)
    def _wait_until_job_is_ready(_base_url: str, _headers: dict[str, str] | None = None):
        check_job_is_ready(_base_url, _headers)

    _wait_until_job_is_ready(base_url, headers)


def check_job_is_alive(
    base_url: str,
    core_api,
    resource_name,
    expected_deployment_timestamp: int,
    headers: dict[str, str] | None,
) -> Response:


    """Check the containers in the pods of the job"""
    pods_by_job = get_job_pods(core_api)
    pods = pods_by_job.get(resource_name, [])

    if not isinstance(pods, list):
        pods = []

    for pod in pods:
        if pod.status:
            container_statuses = getattr(pod.status, 'container_statuses', [])
            if not isinstance(container_statuses, list):
                container_statuses = []

            for container_status in container_statuses:
                # Check both 'state' and 'last_state' for terminated and waiting
                check_state(getattr(container_status.state, 'terminated', None))
                check_state(getattr(container_status.state, 'waiting', None))
                check_state(getattr(container_status.last_state, 'terminated', None))
                check_state(getattr(container_status.last_state, 'waiting', None))

    """Wait until Job resource (pod or container) is up. This catches internal cluster errors"""
    try:
        response = Requests.get(f'{base_url}/live', headers=headers, timeout=3)
    except RequestError as e:
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
            raise RuntimeError("Cluster error: can't reach newer Job, incorrect deployment_timestamp field")

    return response


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


def check_state(state):
    if state:
        reason = getattr(state, 'reason', "No reason")
        message = getattr(state, 'message', "No message")
        if reason in ERRORS_WE_DONT_WANT_IN_REASONS or message in ERRORS_WE_DONT_WANT_IN_MESSAGES:
            raise ValueError(f"Error in {state}: Reason - {reason}, Message - {message}")
