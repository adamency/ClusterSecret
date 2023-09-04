import logging
from typing import Optional, Dict, Any, List

import kopf
import re
from kubernetes import client
import os
from datetime import datetime

from kubernetes.client import CoreV1Api

from consts import CREATE_BY_ANNOTATION, LAST_SYNC_ANNOTATION, \
    VERSION_ANNOTATION


def get_version() -> str:
    return os.getenv('CLUSTER_SECRET_VERSION', '0')


def get_replace_existing() -> bool:
    replace_existing = os.getenv('REPLACE_EXISTING', 'false')
    return replace_existing.lower() == 'true'


def patch_clustersecret_status(
        logger: logging.Logger,
        namespace: str,
        name: str,
        new_status,
        v1: Optional[CoreV1Api] = None
):
    """Patch the status of a given clustersecret object
    """
    v1 = v1 or client.CustomObjectsApi()

    group = 'clustersecret.io'
    version = 'v1'
    plural = 'clustersecrets'

    # Retrieve the clustersecret object
    clustersecret = v1.get_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        name=name
    )

    # Update the status field
    clustersecret['status'] = new_status
    logger.debug(f'Updated clustersecret manifest: {clustersecret}')

    # Perform a patch operation to update the custom resource
    v1.patch_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        name=name,
        body=clustersecret
    )


def get_ns_list(
        logger: logging.Logger,
        body: Dict[str, Any],
        v1: Optional[CoreV1Api] = None
) -> List[str]:
    """Returns a list of namespaces where the secret should be matched
    """
    v1 = v1 or client.CoreV1Api()
    matchNamespace = []
    
    if 'matchNamespace' not in body:
        matchNamespace.append('.*')
    else:
        matchNamespace=body['matchNamespace']
        
    logger.debug(f'Matching namespaces regex: {matchNamespace}')

    if matchNamespace is None:  # if delted key (issue 26)
        matchNamespace.append('.*')

    try:
        avoidNamespaces = body.get('avoidNamespaces')
    except KeyError:
        avoidNamespaces = ''
        logger.debug("not avoiding namespaces")

    nss = v1.list_namespace().items
    matchedns = []
    avoidedns = []

    for matchns in matchNamespace:
        for ns in nss:
            if re.match(matchns, ns.metadata.name):
                matchedns.append(ns.metadata.name)
                logger.debug(f'Matched namespaces: {ns.metadata.name} match pattern: {matchns}')
    if avoidNamespaces:
        for avoidns in avoidNamespaces:
            for ns in nss:
                if re.match(avoidns, ns.metadata.name):
                    avoidedns.append(ns.metadata.name)
                    logger.debug(f'Skipping namespaces: {ns.metadata.name} avoid pattern: {avoidns}')
                    # purge
    for ns in matchedns.copy():
        if ns in avoidedns:
            matchedns.remove(ns)

    return matchedns


def read_data_secret(
        logger: logging.Logger,
        name: str,
        namespace: str,
        v1: Optional[CoreV1Api] = None
):
    """Gets the data from the 'name' secret in namespace
    """
    data = {}
    logger.debug(f'Reading {name} from ns {namespace}')
    try:
        secret = v1.read_namespaced_secret(name, namespace)

        logger.debug(f'Obtained secret {secret}')
        data = secret.data
    except client.exceptions.ApiException as e:
        logger.error('Error reading secret')
        logger.debug(f'error: {e}')
        if e == "404":
            logger.error(f"Secret {name} in ns {namespace} not found!")
        raise kopf.TemporaryError("Error reading secret")
    return data


def delete_secret(
        logger: logging.Logger,
        namespace: str,
        name: str,
        v1: Optional[CoreV1Api] = None
):
    """Deletes a given secret from a given namespace
    """
    v1 = v1 or client.CoreV1Api()

    logger.info(f'deleting secret {name} from namespace {namespace}')
    try:
        v1.delete_namespaced_secret(name, namespace)
    except client.rest.ApiException as e:
        if e.status == 404:
            logger.warning(f"The namespace {namespace} may not exist anymore: Not found")
        else:
            logger.warning(" Something weird deleting the secret")
            logger.debug(f"details: {e}")


def secret_exists(
        logger: logging.Logger,
        name: str,
        namespace: str,
        v1: Optional[CoreV1Api] = None
):
    return secret_metadata(
        logger=logger,
        name=name,
        namespace=namespace,
        v1=v1,
    ) is not None


def secret_metadata(
        logger: logging.Logger,
        name: str,
        namespace: str,
        v1: Optional[CoreV1Api] = None
) -> Optional[Dict[str, str]]:
    v1 = v1 or client.CoreV1Api()

    try:
        secret = v1.read_namespaced_secret(name, namespace)
        return secret.metadata
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        logger.warning(f'Cannot read the secret {e}.')
        raise kopf.TemporaryError(f'Error reading secret {e}')


def sync_secret(
        logger: logging.Logger,
        namespace: str,
        body: Dict[str, Any],
        v1: Optional[CoreV1Api] = None
):
    """Creates a given secret on a given namespace
    """
    v1 = v1 or client.CoreV1Api()

    if 'metadata' not in body:
        raise kopf.TemporaryError("Metadata is required.")

    if 'name' not in body['metadata']:
        raise kopf.TemporaryError("Property name is missing in metadata.")

    sec_name = body['metadata']['name']

    if 'data' not in body:
        raise kopf.TemporaryError("Property data is missing.")

    data = body['data']

    if 'valueFrom' in data:
        if len(data.keys()) > 1:
            logger.error('Data keys with ValueFrom error, enable debug for more details')
            logger.debug(f'keys: {data.keys()}  len {len(data.keys())}')
            raise kopf.TemporaryError("ValueFrom can not coexist with other keys in the data")

        try:
            ns_from = data['valueFrom']['secretKeyRef']['namespace']
            name_from = data['valueFrom']['secretKeyRef']['name']
            # key_from = data['ValueFrom']['secretKeyRef']['name']
            # to-do specifie keys. for now. it will clone all.
            logger.debug(f'Taking value from secret {name_from} from namespace {ns_from} - All keys')
            data = read_data_secret(logger, name_from, ns_from, v1)
        except KeyError:
            logger.error(f'ERROR reading data from remote secret, enable debug for more details')
            logger.debug(f'Deta details: {data}')
            raise kopf.TemporaryError("Can not get Values from external secret")

    logger.debug(f'Going to create with data: {data}')
    secret_type = 'Opaque'
    if 'type' in body:
        secret_type = body['type']

    body = client.V1Secret()
    body.metadata = create_secret_metadata(name=sec_name, namespace=namespace)
    body.type = secret_type
    body.data = data
    # kopf.adopt(body)
    logger.info(f"cloning secret in namespace {namespace}")

    try:
        # Get metadata from secrets (if exist)
        metadata = secret_metadata(logger, name=sec_name, namespace=namespace)

        # If nothing returned, the secret does not exist, creating it then
        if metadata is None:
            logger.info(f'Using create_namespaced_secret')
            logger.debug(f'response is {v1.create_namespaced_secret(namespace, body)}')
            return

        if metadata.annotations is None:
            logger.info(
                "secret `{sec_name}` exist but it does not have annotations, so is not managed by ClusterSecret")

            # If we should not overwrite existing secrets
            if not get_replace_existing():
                logger.info(
                    f"secret `{sec_name}` will not be replaced. You can enforce this by setting env REPLACE_EXISTING to true.")
                return
        else:
            if metadata.annotations.get(CREATE_BY_ANNOTATION) is None:
                logger.error(
                    f"secret `{sec_name}` already exist in namespace '{namespace}' and is not managed by ClusterSecret")

                if not get_replace_existing():
                    logger.info(
                        f"secret `{sec_name}` will not be replaced. You can enforce this by setting env REPLACE_EXISTING to true.")
                    return

            logger.info(f'Reeplacing secret {sec_name}')
            v1.replace_namespaced_secret(
                name=sec_name,
                namespace=namespace,
                body=body
            )
    except client.rest.ApiException as e:
        logger.error(f'Can not create a secret, it is base64 encoded? enable debug for details')
        logger.debug(f'data: {data}')
        logger.debug(f'Kube exception {e}')


def create_secret_metadata(name: str, namespace: str) -> client.V1ObjectMeta:
    return client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        annotations={
            CREATE_BY_ANNOTATION: "ClusterSecrets",
            VERSION_ANNOTATION: get_version(),
            LAST_SYNC_ANNOTATION: datetime.now().isoformat()
        }
    )
