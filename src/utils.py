from __future__ import annotations
from collections import defaultdict
import os

from kubernetes import client
from kubernetes.client import V1ObjectMeta, V1Pod, V1Deployment
from kubernetes.config import load_incluster_config

K8S_NAMESPACE = os.environ.get('JOB_K8S_NAMESPACE', 'racetrack')
K8S_JOB_RESOURCE_LABEL = "racetrack/job"
K8S_JOB_NAME_LABEL = "racetrack/job-name"
K8S_JOB_VERSION_LABEL = "racetrack/job-version"


def k8s_api_client() -> client.ApiClient:
    load_incluster_config()
    return client.ApiClient()


def get_recent_job_pod(pods: list[V1Pod]) -> V1Pod:
    """If many pods are found, return the latest alive pod"""
    assert pods, 'no pod found with expected job label'
    pods_alive = [pod for pod in pods if pod.metadata.deletion_timestamp is None]  # ignore Terminating pods
    assert pods_alive, 'no alive pod found with expected job label'
    recent_pod = sorted(pods_alive, key=lambda pod: pod.metadata.creation_timestamp)[-1]
    return recent_pod


def get_job_pod_names(pods: list[V1Pod]) -> list[str]:
    """Get alive job pods names"""
    assert pods, 'empty pods list'
    pods_alive = [pod for pod in pods if pod.metadata.deletion_timestamp is None]  # ignore Terminating pods
    assert pods_alive, 'no alive pod found'
    return [pod.metadata.name for pod in pods_alive]


def get_job_deployments(apps_api: client.AppsV1Api) -> dict[str, V1Deployment]:
    job_deployments = {}
    _continue = None  # pointer to the query in case of multiple pages
    while True:
        ret = apps_api.list_namespaced_deployment(K8S_NAMESPACE, limit=100, _continue=_continue)
        deployments: list[V1Deployment] = ret.items

        for deployment in deployments:
            metadata: V1ObjectMeta = deployment.metadata
            if K8S_JOB_RESOURCE_LABEL in metadata.labels:
                name = metadata.labels[K8S_JOB_RESOURCE_LABEL]
                job_deployments[name] = deployment

        _continue = ret.metadata._continue
        if _continue is None:
            break

    return job_deployments


def get_job_pods(core_api: client.CoreV1Api) -> dict[str, list[V1Pod]]:
    """Return mapping: resource name (job_name & job_version) -> list of pods"""
    job_pods = defaultdict(list)
    _continue = None  # pointer to the query in case of multiple pages
    while True:
        ret = core_api.list_namespaced_pod(K8S_NAMESPACE, limit=100, _continue=_continue)
        pods: list[V1Pod] = ret.items

        for pod in pods:
            metadata: V1ObjectMeta = pod.metadata
            # omit terminating pods by checking deletion_timestamp,
            # because that's only way to get solid info whether pod has been deleted;
            # pod statuses can have few seconds of delay
            if pod.metadata.deletion_timestamp is not None:
                continue

            if K8S_JOB_RESOURCE_LABEL in metadata.labels:
                name = metadata.labels[K8S_JOB_RESOURCE_LABEL]
                job_pods[name].append(pod)

        _continue = ret.metadata._continue
        if _continue is None:
            break

    return job_pods
